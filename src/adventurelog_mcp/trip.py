"""Read model for one AdventureLog collection ("trip").

Wraps the collection-detail payload and renders the compact, day-by-day shape
the tools return. Datetimes come out as local wall-clock time in the item's own
timezone, because that is how people read and write itineraries.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .timeutil import local_date, to_local

ITEM_TYPES = ("location", "lodging", "transportation", "note", "checklist")

ENDPOINTS = {
    "location": "/api/locations/",
    "lodging": "/api/lodging/",
    "transportation": "/api/transportations/",
    "note": "/api/notes/",
    "checklist": "/api/checklists/",
}

# collection-detail key holding each item type
_COLLECTION_KEYS = {
    "location": "locations",
    "lodging": "lodging",
    "transportation": "transportations",
    "note": "notes",
    "checklist": "checklists",
}

TEXT_LIMIT = 240


def _clip(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def photo(img: dict[str, Any]) -> dict[str, Any]:
    """Compact view of an AdventureLog image: its id (for remove/primary/cover), URL and origin."""
    return {"id": str(img["id"]), "primary": bool(img.get("is_primary")),
            **_drop_empty({"url": img.get("image"), "source": img.get("source"), "source_url": img.get("source_url")})}


class Trip:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.id: str = raw["id"]
        self.name: str = raw["name"]
        self.start_date: str | None = raw.get("start_date")
        self.end_date: str | None = raw.get("end_date")
        self.is_public: bool = bool(raw.get("is_public"))
        self.itinerary: list[dict[str, Any]] = raw.get("itinerary") or []
        self.days: list[dict[str, Any]] = raw.get("itinerary_days") or []
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        for item_type, key in _COLLECTION_KEYS.items():
            for obj in raw.get(key) or []:
                self.objects[(item_type, str(obj["id"]))] = obj
        self.timezone = self._guess_timezone()

    # ------------------------------------------------------------------ lookups

    def _guess_timezone(self) -> str:
        """Most common timezone among lodging, visits and arrivals: the trip's 'home' zone."""
        counts: Counter[str] = Counter()
        for (item_type, _), obj in self.objects.items():
            if item_type == "lodging" and obj.get("timezone"):
                counts[obj["timezone"]] += 2
            elif item_type == "location":
                for v in obj.get("visits") or []:
                    if v.get("timezone"):
                        counts[v["timezone"]] += 1
            elif item_type == "transportation" and obj.get("end_timezone"):
                counts[obj["end_timezone"]] += 1
        return counts.most_common(1)[0][0] if counts else "UTC"

    def replace_itinerary(self, updated: list[dict[str, Any]]) -> None:
        """Merge itinerary rows returned by the API (e.g. from reorder) into the in-memory copy."""
        by_id = {str(u["id"]): u for u in updated}
        self.itinerary = [by_id.pop(str(it["id"]), it) for it in self.itinerary] + list(by_id.values())

    def item_type_of(self, itin: dict[str, Any]) -> str:
        return (itin.get("item") or {}).get("type") or itin.get("object_name") or "unknown"

    def itinerary_for(self, object_id: str) -> list[dict[str, Any]]:
        return [it for it in self.itinerary if str(it["object_id"]) == str(object_id)]

    def itinerary_item(self, itinerary_id: str) -> dict[str, Any] | None:
        return next((it for it in self.itinerary if str(it["id"]) == str(itinerary_id)), None)

    def group(self, date: str | None) -> list[dict[str, Any]]:
        """Itinerary items on a date (or trip-wide when date is None), in display order."""
        if date is None:
            items = [it for it in self.itinerary if it.get("is_global")]
        else:
            items = [it for it in self.itinerary if not it.get("is_global") and it.get("date") == date]
        return sorted(items, key=lambda it: (it.get("order") or 0))

    def day_meta(self, date: str) -> dict[str, Any] | None:
        return next((d for d in self.days if d.get("date") == date), None)

    def object(self, item_type: str, object_id: str) -> dict[str, Any] | None:
        return self.objects.get((item_type, str(object_id)))

    # ------------------------------------------------------------------ rendering

    def render_object(self, item_type: str, obj: dict[str, Any], on_date: str | None = None) -> dict[str, Any]:
        tz = self.timezone
        base: dict[str, Any] = {"type": item_type, "id": str(obj["id"]), "name": obj.get("name")}
        if item_type == "location":
            visit = None
            for v in obj.get("visits") or []:
                vtz = v.get("timezone") or tz
                if on_date is None or local_date(v.get("start_date"), vtz) == on_date:
                    visit = {"start": to_local(v.get("start_date"), vtz), "end": to_local(v.get("end_date"), vtz), "timezone": vtz}
                    break
            cat = obj.get("category") or {}
            base.update(
                category=cat.get("display_name") or cat.get("name"),
                address=obj.get("location"),
                latitude=obj.get("latitude"),
                longitude=obj.get("longitude"),
                visit=visit,
                price=obj.get("price"),
                link=obj.get("link"),
                description=_clip(obj.get("description")),
            )
        elif item_type == "lodging":
            ltz = obj.get("timezone") or tz
            base.update(
                lodging_type=obj.get("type"),
                check_in=to_local(obj.get("check_in"), ltz),
                check_out=to_local(obj.get("check_out"), ltz),
                timezone=ltz,
                address=obj.get("location"),
                reservation_number=obj.get("reservation_number"),
                price=obj.get("price"),
                price_currency=obj.get("price_currency") if obj.get("price") else None,
                description=_clip(obj.get("description")),
            )
        elif item_type == "transportation":
            stz = obj.get("start_timezone") or tz
            etz = obj.get("end_timezone") or stz
            base.update(
                transport_type=obj.get("type"),
                flight_number=obj.get("flight_number"),
                from_location=obj.get("from_location"),
                to_location=obj.get("to_location"),
                start_code=obj.get("start_code"),
                end_code=obj.get("end_code"),
                depart=to_local(obj.get("date"), stz),
                arrive=to_local(obj.get("end_date"), etz),
                start_timezone=stz,
                end_timezone=etz,
                description=_clip(obj.get("description")),
            )
        elif item_type == "note":
            base.update(content=_clip(obj.get("content"), 400), links=obj.get("links"))
        elif item_type == "checklist":
            base.update(
                items=[
                    {"id": str(i["id"]), "name": i.get("name"), "checked": bool(i.get("is_checked"))}
                    for i in obj.get("items") or []
                ]
            )
        base["photos"] = len([i for i in obj.get("images") or [] if i]) or None
        return _drop_empty(base)

    def render_itinerary_entry(self, itin: dict[str, Any]) -> dict[str, Any]:
        item_type = self.item_type_of(itin)
        obj = self.object(item_type, str(itin["object_id"]))
        entry: dict[str, Any] = {"itinerary_id": str(itin["id"]), "order": itin.get("order")}
        if obj is None:
            entry.update(type=item_type, id=str(itin["object_id"]), missing=True)
        else:
            entry.update(self.render_object(item_type, obj, None if itin.get("is_global") else itin.get("date")))
        return entry

    def summary(self) -> dict[str, Any]:
        raw = self.raw
        return _drop_empty(
            {
                "id": self.id,
                "name": self.name,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "status": raw.get("status"),
                "is_public": self.is_public,
                "timezone": self.timezone,
                "collaborators": [
                    {"username": c.get("username"), "owner": bool(c.get("is_owner"))}
                    for c in raw.get("collaborators") or []
                ],
                "updated_at": raw.get("updated_at"),
                "cover_photo": photo(raw["primary_image"]) if raw.get("primary_image") else None,
            }
        )

    def render(self) -> dict[str, Any]:
        dates = sorted(
            {d["date"] for d in self.days if d.get("date")}
            | {it["date"] for it in self.itinerary if it.get("date") and not it.get("is_global")}
        )
        days = []
        for date in dates:
            meta = self.day_meta(date) or {}
            days.append(
                _drop_empty(
                    {
                        "date": date,
                        "day_id": meta.get("id"),
                        "name": meta.get("name"),
                        "description": meta.get("description"),
                        "items": [self.render_itinerary_entry(it) for it in self.group(date)],
                    }
                )
            )
        scheduled = {(self.item_type_of(it), str(it["object_id"])) for it in self.itinerary}
        unscheduled = [
            self.render_object(t, obj) for (t, oid), obj in self.objects.items() if (t, oid) not in scheduled
        ]
        out = self.summary()
        out.update(
            description=self.raw.get("description") or None,
            days=days,
            trip_wide=[self.render_itinerary_entry(it) for it in self.group(None)],
            unscheduled=unscheduled,
        )
        return _drop_empty(out)
