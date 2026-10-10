"""Trip-planning operations on top of the AdventureLog REST API.

Kept free of MCP types so it can be unit-tested and reused (e.g. by the bulk
`apply_trip_plan` diff engine).
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from typing import Any

from .client import AdventureLogClient, ApiError
from .timeutil import check_date, local_date, parse_utc, to_utc_iso
from .trip import ENDPOINTS, ITEM_TYPES, Trip


class PlanError(ValueError):
    """A request the tools refuse before calling the API (bad combination of arguments)."""


def _set(payload: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Add only the fields the caller actually supplied (partial-update semantics)."""
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


def _shift_utc(value: str | None, days: int) -> str | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    return (dt + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TripService:
    def __init__(self, client: AdventureLogClient):
        self.api = client

    # ------------------------------------------------------------------ reads

    async def load_trip(self, trip_id: str) -> Trip:
        return Trip(await self.api.get(f"/api/collections/{trip_id}/"))

    async def list_trips(self, include_shared: bool = True, include_archived: bool = False) -> list[dict[str, Any]]:
        owned = await self.api.get_all("/api/collections/")
        shared = await self.api.get_all("/api/collections/shared/") if include_shared else []
        out = []
        for c, is_shared in [(c, False) for c in owned] + [(c, True) for c in shared]:
            if c.get("is_archived") and not include_archived:
                continue
            out.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "start_date": c.get("start_date"),
                    "end_date": c.get("end_date"),
                    "status": c.get("status"),
                    "location_count": c.get("location_count"),
                    "is_public": c.get("is_public"),
                    "shared_with_me": is_shared,
                }
            )
        return sorted(out, key=lambda t: t.get("start_date") or "9999")

    async def get_item(self, item_type: str, item_id: str) -> dict[str, Any]:
        obj = await self.api.get(f"{ENDPOINTS[_check_type(item_type)]}{item_id}/")
        for noisy in ("images", "attachments", "trails", "user"):
            obj.pop(noisy, None)
        for geo in ("city", "region", "country"):
            if isinstance(obj.get(geo), dict):
                obj[geo] = obj[geo].get("name") or obj[geo].get("id")
        return obj

    async def search_places(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        hits = await self.api.get("/api/places/search/", query=query)
        return [
            {
                "name": h.get("name"),
                "address": h.get("display_name"),
                "latitude": round(float(h["lat"]), 6),
                "longitude": round(float(h["lon"]), 6),
                "kind": h.get("type") or h.get("category"),
                "website": h.get("website"),
            }
            for h in (hits or [])[:limit]
            if h.get("lat") is not None and h.get("lon") is not None
        ]

    # ------------------------------------------------------------------ trips + days

    async def create_trip(self, name: str, start_date: str | None, end_date: str | None,
                          description: str | None, is_public: bool) -> Trip:
        payload = _set({"name": name, "is_public": is_public}, description=description,
                       start_date=start_date and check_date(start_date),
                       end_date=end_date and check_date(end_date))
        created = await self.api.post("/api/collections/", payload)
        return await self.load_trip(created["id"])

    async def update_trip(self, trip_id: str, **fields: Any) -> Trip:
        payload = _set({}, **fields)
        if not payload:
            raise PlanError("Nothing to update.")
        await self.api.patch(f"/api/collections/{trip_id}/", payload)
        return await self.load_trip(trip_id)

    async def delete_trip(self, trip_id: str, confirm_name: str) -> dict[str, Any]:
        trip = await self.load_trip(trip_id)
        if confirm_name.strip() != trip.name.strip():
            raise PlanError(f"confirm_name does not match the trip name {trip.name!r}; nothing deleted.")
        await self.api.delete(f"/api/collections/{trip_id}/")
        return {"deleted": True, "id": trip_id, "name": trip.name}

    async def upsert_day(self, trip_id: str, date: str, name: str | None, description: str | None) -> dict[str, Any]:
        date = check_date(date)
        trip = await self.load_trip(trip_id)
        existing = trip.day_meta(date)
        payload = _set({}, name=name, description=description)
        if existing:
            day = await self.api.patch(f"/api/itinerary-days/{existing['id']}/", payload)
        else:
            day = await self.api.post("/api/itinerary-days/", {"collection": trip_id, "date": date, **payload})
        return {"day_id": day["id"], "date": day["date"], "name": day.get("name"), "description": day.get("description")}

    # ------------------------------------------------------------------ scheduling

    async def schedule(self, trip: Trip, item_type: str, object_id: str,
                       date: str | None, trip_wide: bool) -> dict[str, Any]:
        """Make sure the object appears on `date` (or trip-wide). Moves a single existing
        entry rather than duplicating it; adds a new entry if it is already on several days."""
        if trip_wide and date:
            raise PlanError("Give either date or trip_wide, not both.")
        existing = trip.itinerary_for(object_id)
        if trip_wide:
            hit = next((it for it in existing if it.get("is_global")), None)
            if hit:
                return _entry(hit)
            if len(existing) == 1:
                return await self._regroup(trip, existing[0], None)
            created = await self.api.post("/api/itineraries/", {
                "collection": trip.id, "content_type": item_type, "object_id": object_id, "is_global": True,
                "order": _next_order(trip.group(None))})
            return _entry(created)
        date = _in_trip(trip, date)  # type: ignore[arg-type]
        hit = next((it for it in existing if not it.get("is_global") and it.get("date") == date), None)
        if hit:
            return _entry(hit)
        if len(existing) == 1:
            return await self._regroup(trip, existing[0], date)
        created = await self.api.post("/api/itineraries/", {
            "collection": trip.id, "content_type": item_type, "object_id": object_id, "date": date,
            "order": _next_order(trip.group(date))})
        return _entry(created)

    async def _regroup(self, trip: Trip, itin: dict[str, Any], date: str | None,
                       position: int | None = None) -> dict[str, Any]:
        """Move an itinerary entry to `date` (None = trip-wide) at `position`, renumbering the target group."""
        group = [it for it in trip.group(date) if it["id"] != itin["id"]]
        position = len(group) if position is None else max(0, min(position, len(group)))
        group.insert(position, itin)
        payload = []
        for order, it in enumerate(group):
            row: dict[str, Any] = {"id": str(it["id"]), "order": order}
            if date is None:
                row.update(is_global=True, date=None)
            else:
                row.update(is_global=False, date=date)
            payload.append(row)
        updated = await self.api.post("/api/itineraries/reorder/", {"items": payload})
        moved = next(u for u in updated if str(u["id"]) == str(itin["id"]))
        return _entry(moved)

    async def move_item(self, itinerary_id: str, date: str | None, trip_wide: bool,
                        position: int | None, shift_times: bool) -> dict[str, Any]:
        if trip_wide == bool(date):
            raise PlanError("Give exactly one of date or trip_wide.")
        itin = await self.api.get(f"/api/itineraries/{itinerary_id}/")
        trip = await self.load_trip(str(itin["collection"]))
        itin = trip.itinerary_item(itinerary_id) or itin
        new_date = None if trip_wide else _in_trip(trip, date)  # type: ignore[arg-type]
        old_date = None if itin.get("is_global") else itin.get("date")
        result = await self._regroup(trip, itin, new_date, position)
        if shift_times and old_date and new_date and old_date != new_date:
            delta = (Date.fromisoformat(new_date) - Date.fromisoformat(old_date)).days
            await self._shift_object(trip, trip.item_type_of(itin), str(itin["object_id"]), old_date, delta)
            result["times_shifted_days"] = delta
        return result

    async def _shift_object(self, trip: Trip, item_type: str, object_id: str, old_date: str, days: int) -> None:
        obj = trip.object(item_type, object_id)
        if obj is None:
            return
        path = f"{ENDPOINTS[item_type]}{object_id}/"
        if item_type == "transportation":
            await self.api.patch(path, _set({}, date=_shift_utc(obj.get("date"), days),
                                            end_date=_shift_utc(obj.get("end_date"), days)))
        elif item_type == "lodging":
            await self.api.patch(path, _set({}, check_in=_shift_utc(obj.get("check_in"), days),
                                            check_out=_shift_utc(obj.get("check_out"), days)))
        elif item_type in ("note", "checklist"):
            if obj.get("date"):
                new = (Date.fromisoformat(obj["date"][:10]) + timedelta(days=days)).isoformat()
                await self.api.patch(path, {"date": new})
        elif item_type == "location":
            visits = []
            for v in obj.get("visits") or []:
                tz = v.get("timezone") or trip.timezone
                if local_date(v.get("start_date"), tz) == old_date:
                    v = {**v, "start_date": _shift_utc(v["start_date"], days),
                         "end_date": _shift_utc(v.get("end_date"), days)}
                visits.append(_visit_payload(v))
            await self.api.patch(path, {"visits": visits})

    async def reorder_day(self, trip_id: str, date: str | None, itinerary_ids: list[str]) -> list[dict[str, Any]]:
        trip = await self.load_trip(trip_id)
        date = date and check_date(date)
        current = {str(it["id"]) for it in trip.group(date)}
        given = [str(i) for i in itinerary_ids]
        if set(given) != current or len(given) != len(current):
            missing, extra = current - set(given), set(given) - current
            raise PlanError(
                f"itinerary_ids must list every entry on {date or 'trip-wide'} exactly once. "
                f"Missing: {sorted(missing)}; not on that day: {sorted(extra)}"
            )
        payload = [{"id": i, "order": n, "date": date, "is_global": date is None} for n, i in enumerate(given)]
        updated = await self.api.post("/api/itineraries/reorder/", {"items": payload})
        return [_entry(u) for u in sorted(updated, key=lambda u: u["order"])]

    async def unschedule(self, itinerary_id: str, keep_visit: bool) -> dict[str, Any]:
        await self.api.delete(f"/api/itineraries/{itinerary_id}/",
                              preserve_visits="true" if keep_visit else "false")
        return {"unscheduled": True, "itinerary_id": itinerary_id}

    # ------------------------------------------------------------------ elements

    async def delete_item(self, item_type: str, item_id: str) -> dict[str, Any]:
        await self.api.delete(f"{ENDPOINTS[_check_type(item_type)]}{item_id}/")
        return {"deleted": True, "type": item_type, "id": item_id}

    async def _save(self, item_type: str, item_id: str | None, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if item_id:
            return await self.api.patch(f"{ENDPOINTS[item_type]}{item_id}/", payload), False
        return await self.api.post(ENDPOINTS[item_type], payload), True

    async def _finish(self, trip_id: str | None, item_type: str, obj: dict[str, Any], created: bool,
                      date: str | None, trip_wide: bool, trip: Trip | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"type": item_type, "id": str(obj["id"]), "name": obj.get("name"), "created": created}
        if trip_id and (date or trip_wide):
            trip = trip or await self.load_trip(trip_id)
            result["scheduled"] = await self.schedule(trip, item_type, str(obj["id"]), date, trip_wide)
        return result

    async def _trip_or_none(self, trip_id: str | None) -> Trip | None:
        return await self.load_trip(trip_id) if trip_id else None

    async def upsert_location(self, *, trip_id: str | None, location_id: str | None, name: str | None,
                              description: str | None, category: str | None, category_icon: str | None,
                              latitude: float | None, longitude: float | None, address: str | None,
                              link: str | None, price: float | None, price_currency: str | None,
                              visit_start: str | None, visit_end: str | None, timezone: str | None,
                              date: str | None, trip_wide: bool) -> dict[str, Any]:
        if not location_id and not name:
            raise PlanError("name is required when creating a location.")
        trip = await self._trip_or_none(trip_id)
        tz = timezone or (trip.timezone if trip else "UTC")
        payload = _set({}, name=name, description=description, location=address, link=link,
                       latitude=_coord(latitude), longitude=_coord(longitude),
                       price=price, price_currency=price_currency)
        if category:
            payload["category"] = {"name": category.strip().lower(), "display_name": category.strip(),
                                   "icon": category_icon or "📍"}
        existing = None
        if location_id:
            existing = await self.api.get(f"{ENDPOINTS['location']}{location_id}/")
            if trip_id and trip_id not in [str(c) for c in existing.get("collections") or []]:
                payload["collections"] = [*map(str, existing.get("collections") or []), trip_id]
        else:
            payload["is_public"] = bool(trip and trip.is_public)
            if trip_id:
                payload["collections"] = [trip_id]
        obj, created = await self._save("location", location_id, payload)

        if visit_start:
            visit_date = local_date(to_utc_iso(visit_start, tz), tz)
            date = date or visit_date
            old = existing or obj
            visits = [
                _visit_payload(v) for v in old.get("visits") or []
                if local_date(v.get("start_date"), v.get("timezone") or tz) != visit_date
            ]
            visits.append({
                "start_date": to_utc_iso(visit_start, tz),
                "end_date": to_utc_iso(visit_end or visit_start, tz),
                "timezone": tz,
            })
            obj = await self.api.patch(f"{ENDPOINTS['location']}{obj['id']}/", {"visits": visits})
        return await self._finish(trip_id, "location", obj, created, date, trip_wide, trip)

    async def upsert_lodging(self, *, trip_id: str | None, lodging_id: str | None, name: str | None,
                             lodging_type: str | None, check_in: str | None, check_out: str | None,
                             timezone: str | None, address: str | None, latitude: float | None,
                             longitude: float | None, reservation_number: str | None, price: float | None,
                             price_currency: str | None, link: str | None, description: str | None,
                             date: str | None, trip_wide: bool) -> dict[str, Any]:
        if not lodging_id and not name:
            raise PlanError("name is required when creating lodging.")
        trip = await self._trip_or_none(trip_id)
        tz = timezone or (trip.timezone if trip else "UTC")
        payload = _set({}, name=name, type=lodging_type, location=address, reservation_number=reservation_number,
                       latitude=_coord(latitude), longitude=_coord(longitude), price=price,
                       price_currency=price_currency, link=link, description=description,
                       check_in=check_in and to_utc_iso(check_in, tz),
                       check_out=check_out and to_utc_iso(check_out, tz),
                       timezone=(timezone or (tz if check_in or check_out else None)), collection=trip_id)
        if not lodging_id:
            payload.setdefault("type", "hotel")
            payload["is_public"] = bool(trip and trip.is_public)
        obj, created = await self._save("lodging", lodging_id, payload)
        if trip_id and not date and not trip_wide and obj.get("check_in"):
            date = local_date(obj["check_in"], obj.get("timezone") or tz)
        return await self._finish(trip_id, "lodging", obj, created, date, trip_wide, trip)

    async def upsert_transportation(self, *, trip_id: str | None, transportation_id: str | None,
                                    name: str | None, transport_type: str | None, depart: str | None,
                                    arrive: str | None, start_timezone: str | None, end_timezone: str | None,
                                    from_location: str | None, to_location: str | None,
                                    start_code: str | None, end_code: str | None, flight_number: str | None,
                                    origin_latitude: float | None, origin_longitude: float | None,
                                    destination_latitude: float | None, destination_longitude: float | None,
                                    price: float | None, price_currency: str | None, link: str | None,
                                    description: str | None, date: str | None, trip_wide: bool) -> dict[str, Any]:
        if not transportation_id and not (name and transport_type):
            raise PlanError("name and transport_type are required when creating transportation.")
        trip = await self._trip_or_none(trip_id)
        stz = start_timezone or (trip.timezone if trip else "UTC")
        etz = end_timezone or stz
        payload = _set({}, name=name, type=transport_type, from_location=from_location, to_location=to_location,
                       start_code=start_code, end_code=end_code, flight_number=flight_number,
                       origin_latitude=_coord(origin_latitude), origin_longitude=_coord(origin_longitude),
                       destination_latitude=_coord(destination_latitude),
                       destination_longitude=_coord(destination_longitude),
                       price=price, price_currency=price_currency, link=link, description=description,
                       date=depart and to_utc_iso(depart, stz), end_date=arrive and to_utc_iso(arrive, etz),
                       start_timezone=start_timezone or (stz if depart else None),
                       end_timezone=end_timezone or (etz if arrive else None), collection=trip_id)
        if not transportation_id:
            payload["is_public"] = bool(trip and trip.is_public)
        obj, created = await self._save("transportation", transportation_id, payload)
        if trip_id and not date and not trip_wide and obj.get("date"):
            date = local_date(obj["date"], obj.get("start_timezone") or stz)
        return await self._finish(trip_id, "transportation", obj, created, date, trip_wide, trip)

    async def upsert_note(self, *, trip_id: str | None, note_id: str | None, name: str | None,
                          content: str | None, links: list[str] | None, date: str | None,
                          trip_wide: bool) -> dict[str, Any]:
        if not note_id and not name:
            raise PlanError("name is required when creating a note.")
        trip = await self._trip_or_none(trip_id)
        payload = _set({}, name=name, content=content, links=links, collection=trip_id,
                       date=date and check_date(date))
        if not note_id:
            payload["is_public"] = bool(trip and trip.is_public)
        obj, created = await self._save("note", note_id, payload)
        return await self._finish(trip_id, "note", obj, created, date, trip_wide, trip)

    async def upsert_checklist(self, *, trip_id: str | None, checklist_id: str | None, name: str | None,
                               items: list[dict[str, Any]] | None, date: str | None,
                               trip_wide: bool) -> dict[str, Any]:
        if not checklist_id and not name:
            raise PlanError("name is required when creating a checklist.")
        trip = await self._trip_or_none(trip_id)
        payload = _set({}, name=name, collection=trip_id, date=date and check_date(date))
        if items is not None:
            payload["items"] = [
                _set({"name": i["name"], "is_checked": bool(i.get("checked", False))}, id=i.get("id"))
                for i in items
            ]
        if not checklist_id:
            payload.setdefault("items", [])
            payload["is_public"] = bool(trip and trip.is_public)
        obj, created = await self._save("checklist", checklist_id, payload)
        return await self._finish(trip_id, "checklist", obj, created, date, trip_wide, trip)

    # ------------------------------------------------------------------ sharing

    async def share_trip(self, trip_id: str, username: str) -> dict[str, Any]:
        """Send a collaboration invite. AdventureLog only lets you invite users with a public profile."""
        try:
            user = await self.api.get(f"/auth/user/{username}/")
        except ApiError as exc:
            if exc.status == 404:
                raise PlanError(
                    f"No invitable user {username!r}. AdventureLog only allows inviting users whose "
                    "profile is public (Settings -> public profile)."
                ) from exc
            raise
        resp = await self.api.post(f"/api/collections/{trip_id}/share/{user['uuid']}/", {})
        return {"invited": True, "trip_id": trip_id, "username": username,
                "note": (resp or {}).get("success", "Invite sent; they must accept it in AdventureLog.")}

    async def unshare_trip(self, trip_id: str, username: str) -> dict[str, Any]:
        trip = await self.load_trip(trip_id)
        match = next((c for c in trip.raw.get("collaborators") or []
                      if c.get("username") == username and not c.get("is_owner")), None)
        if not match:
            raise PlanError(f"{username!r} is not a collaborator on this trip.")
        await self.api.post(f"/api/collections/{trip_id}/unshare/{match['uuid']}/", {})
        return {"unshared": True, "trip_id": trip_id, "username": username}

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        resp = await self.api.get("/api/search/", query=query, limit=limit)
        return [
            {"type": r.get("type"), "id": r.get("id"), "name": r.get("title"), "detail": r.get("subtitle")}
            for r in (resp or {}).get("results", [])
        ]


def _check_type(item_type: str) -> str:
    if item_type not in ITEM_TYPES:
        raise PlanError(f"item_type must be one of {', '.join(ITEM_TYPES)}")
    return item_type


def _in_trip(trip: Trip, date: str) -> str:
    date = check_date(date)
    if (trip.start_date and date < trip.start_date) or (trip.end_date and date > trip.end_date):
        raise PlanError(
            f"{date} is outside the trip ({trip.start_date} to {trip.end_date}). "
            "Change the trip dates with update_trip first if the trip is getting longer."
        )
    return date


def _next_order(group: list[dict[str, Any]]) -> int:
    return max((it.get("order") or 0 for it in group), default=-1) + 1


def _coord(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _visit_payload(v: dict[str, Any]) -> dict[str, Any]:
    # No "location" key: LocationSerializer.update() adds it itself and a duplicate 500s.
    return _set({"start_date": v.get("start_date"), "end_date": v.get("end_date")},
                timezone=v.get("timezone"), notes=v.get("notes"))


def _entry(itin: dict[str, Any]) -> dict[str, Any]:
    return {
        "itinerary_id": str(itin["id"]),
        "date": None if itin.get("is_global") else itin.get("date"),
        "trip_wide": bool(itin.get("is_global")),
        "order": itin.get("order"),
    }
