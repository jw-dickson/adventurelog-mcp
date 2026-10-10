"""Declarative trip plans: diff a whole plan against a live trip, then apply exactly that diff.

A plan states what the trip should look like: trip fields, day titles, and per day the
ordered items. Items are matched to existing ones by explicit `id`, else by
(type, normalised name), so re-running the same plan is a no-op. Only fields the plan
states are compared and written, so anything the plan leaves out is kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .client import ApiError
from .service import PlanError, TripService, _in_trip
from .timeutil import local_date, to_utc_iso
from .trip import Trip

# ---------------------------------------------------------------------------- schema


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    id: str | None = Field(default=None, description="Existing item id; otherwise matched by type + name.")
    description: str | None = None


class PlanLocation(_Item):
    type: Literal["location"] = "location"
    category: str | None = None
    category_icon: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    link: str | None = None
    price: float | None = None
    price_currency: str | None = None
    start: str | None = Field(default=None, description="Visit start: 'HH:MM' on this day, or 'YYYY-MM-DDTHH:MM'.")
    end: str | None = Field(default=None, description="Visit end, same formats as start.")
    timezone: str | None = None


class PlanLodging(_Item):
    type: Literal["lodging"] = "lodging"
    lodging_type: str | None = None
    check_in: str | None = Field(default=None, description="'YYYY-MM-DDTHH:MM' local")
    check_out: str | None = None
    timezone: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    reservation_number: str | None = None
    price: float | None = None
    price_currency: str | None = None
    link: str | None = None


class PlanTransportation(_Item):
    type: Literal["transportation"] = "transportation"
    transport_type: str | None = Field(default=None, description="car, plane, train, bus, boat, bike, walking, other")
    depart: str | None = Field(default=None, description="'YYYY-MM-DDTHH:MM' local in start_timezone")
    arrive: str | None = Field(default=None, description="'YYYY-MM-DDTHH:MM' local in end_timezone")
    start_timezone: str | None = None
    end_timezone: str | None = None
    from_location: str | None = None
    to_location: str | None = None
    start_code: str | None = None
    end_code: str | None = None
    flight_number: str | None = None
    origin_latitude: float | None = None
    origin_longitude: float | None = None
    destination_latitude: float | None = None
    destination_longitude: float | None = None
    price: float | None = None
    price_currency: str | None = None
    link: str | None = None


class PlanNote(_Item):
    type: Literal["note"] = "note"
    content: str | None = None
    links: list[str] | None = None


class PlanChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    checked: bool = False


class PlanChecklist(_Item):
    type: Literal["checklist"] = "checklist"
    items: list[PlanChecklistItem] | None = None


PlanItem = Annotated[
    PlanLocation | PlanLodging | PlanTransportation | PlanNote | PlanChecklist, Field(discriminator="type")
]


class PlanDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="YYYY-MM-DD")
    name: str | None = None
    description: str | None = None
    items: list[PlanItem] = Field(default_factory=list, description="In display order")


class TripPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_public: bool | None = None
    timezone: str | None = Field(default=None, description="Default IANA timezone for times in the plan.")
    trip_wide: list[PlanItem] = Field(default_factory=list)
    days: list[PlanDay] = Field(default_factory=list)


# ---------------------------------------------------------------------------- normalisation


def norm_name(name: str | None) -> str:
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


class _CategoryNames(tuple):
    """A category matches by slug or display name."""

    def __new__(cls, slug: str | None, display: str | None):
        return super().__new__(cls, ((slug or "").casefold(), (display or "").casefold()))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and other.casefold() in self

    __hash__ = tuple.__hash__


def _same(old: Any, new: Any) -> bool:
    if isinstance(new, float) or isinstance(old, float):
        try:
            return abs(float(old) - float(new)) < 1e-5
        except (TypeError, ValueError):
            return False
    if isinstance(new, (int, Decimal)) and not isinstance(new, bool):
        try:
            return Decimal(str(old)) == Decimal(str(new))
        except (InvalidOperation, TypeError):
            return False
    if isinstance(new, str) and isinstance(old, str):
        return old.strip() == new.strip()
    return old == new


def _desired(item: Any, day: str | None, tz: str) -> dict[str, Any]:
    """The raw API fields (and values) this plan item asserts."""
    t = item.type
    d: dict[str, Any] = {"name": item.name}
    if item.description is not None and t in ("location", "lodging", "transportation"):
        d["description"] = item.description
    if t == "location":
        d.update(_pick(item, location="address", link="link", latitude="latitude", longitude="longitude",
                       price="price", price_currency="price_currency"))
        if item.category:
            d["category"] = item.category.strip().casefold()
        if item.start:
            ltz = item.timezone or tz
            d["visit"] = (to_utc_iso(_on_day(item.start, day), ltz), to_utc_iso(_on_day(item.end or item.start, day), ltz))
    elif t == "lodging":
        ltz = item.timezone or tz
        d.update(_pick(item, type="lodging_type", location="address", latitude="latitude", longitude="longitude",
                       reservation_number="reservation_number", price="price", price_currency="price_currency",
                       link="link"))
        if item.check_in:
            d["check_in"] = to_utc_iso(item.check_in, ltz)
        if item.check_out:
            d["check_out"] = to_utc_iso(item.check_out, ltz)
        if item.check_in or item.check_out or item.timezone:
            d["timezone"] = ltz
    elif t == "transportation":
        stz = item.start_timezone or tz
        etz = item.end_timezone or stz
        d.update(_pick(item, type="transport_type", from_location="from_location", to_location="to_location",
                       start_code="start_code", end_code="end_code", flight_number="flight_number",
                       origin_latitude="origin_latitude", origin_longitude="origin_longitude",
                       destination_latitude="destination_latitude", destination_longitude="destination_longitude",
                       price="price", price_currency="price_currency", link="link"))
        if item.depart:
            d["date"], d["start_timezone"] = to_utc_iso(item.depart, stz), stz
        if item.arrive:
            d["end_date"], d["end_timezone"] = to_utc_iso(item.arrive, etz), etz
    elif t == "note":
        d.update(_pick(item, content="content", links="links"))
    elif t == "checklist" and item.items is not None:
        d["items"] = [(i.name.strip(), i.checked) for i in item.items]
    return d


def _pick(item: Any, **mapping: str) -> dict[str, Any]:
    return {raw: getattr(item, attr) for raw, attr in mapping.items() if getattr(item, attr) is not None}


def _on_day(value: str, day: str | None) -> str:
    if re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        if not day:
            raise PlanError(f"Time {value!r} needs a day; use 'YYYY-MM-DDTHH:MM' for trip-wide items.")
        return f"{day}T{value.strip().zfill(5)}"
    return value


def _current(raw: dict[str, Any], key: str, day: str | None, tz: str) -> Any:
    if key == "category":
        cat = raw.get("category") or {}
        return _CategoryNames(cat.get("name"), cat.get("display_name"))
    if key == "items":
        return [((i.get("name") or "").strip(), bool(i.get("is_checked"))) for i in raw.get("items") or []]
    if key == "visit":
        for v in raw.get("visits") or []:
            if day is None or local_date(v.get("start_date"), v.get("timezone") or tz) == day:
                return (_z(v.get("start_date")), _z(v.get("end_date")))
        return None
    if key in ("check_in", "check_out", "date", "end_date"):
        return _z(raw.get(key))
    return raw.get(key)


def _z(value: str | None) -> str | None:
    """Normalise API datetimes ('...T03:10:00Z' / '+00:00' / fractional) to the to_utc_iso format."""
    if not value:
        return None
    return value.replace("+00:00", "Z").split(".")[0].rstrip("Z") + "Z"


# ---------------------------------------------------------------------------- diff


class PlanDiff:
    def __init__(self, plan: TripPlan, trip: Trip | None):
        self.plan = plan
        self.trip = trip
        self.tz = plan.timezone or (trip.timezone if trip else "UTC")
        self.trip_op: dict[str, Any] = {}
        self.day_ops: list[dict[str, Any]] = []
        self.item_ops: list[dict[str, Any]] = []
        self.not_in_plan: list[dict[str, Any]] = []
        self._compute()

    # placement = (date or None for trip-wide)
    def _placements(self) -> list[tuple[str | None, Any]]:
        out: list[tuple[str | None, Any]] = [(None, i) for i in self.plan.trip_wide]
        for day in self.plan.days:
            out += [(day.date, i) for i in day.items]
        return out

    def _compute(self) -> None:
        plan, trip = self.plan, self.trip
        start = plan.start_date or (trip.start_date if trip else None)
        end = plan.end_date or (trip.end_date if trip else None)
        for day in plan.days:
            if (start and day.date < start) or (end and day.date > end):
                raise PlanError(f"Day {day.date} is outside the trip dates {start} to {end}; set start_date/end_date.")

        # trip fields
        wanted = {k: getattr(plan, k) for k in ("name", "description", "start_date", "end_date", "is_public")
                  if getattr(plan, k) is not None}
        if trip is None:
            if not plan.name:
                raise PlanError("A new trip needs a name.")
            self.trip_op = {"action": "create", "fields": wanted}
        else:
            changes = {k: [trip.raw.get(k), v] for k, v in wanted.items() if not _same(trip.raw.get(k), v)}
            self.trip_op = {"action": "update" if changes else "unchanged", "changes": changes}

        # day titles
        for day in plan.days:
            meta = (trip.day_meta(day.date) if trip else None) or {}
            changes = {k: [meta.get(k), getattr(day, k)] for k in ("name", "description")
                       if getattr(day, k) is not None and not _same(meta.get(k), getattr(day, k))}
            if changes:
                self.day_ops.append({"date": day.date, "changes": changes})

        # items
        existing: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if trip:
            for (t, oid), obj in trip.objects.items():
                existing.setdefault((t, norm_name(obj.get("name"))), []).append(obj)
        claimed: dict[tuple[str, str], str] = {}  # (type, norm name) -> object id chosen for the first occurrence
        used_ids: set[str] = set()
        for index, (day, item) in enumerate(self._placements()):
            key = (item.type, norm_name(item.name))
            obj = None
            if item.id:
                obj = trip.object(item.type, item.id) if trip else None
                if obj is None:
                    raise PlanError(f"{item.type} id {item.id} ({item.name!r}) is not in this trip.")
            elif key in claimed:
                obj = trip.object(item.type, claimed[key]) if trip else None
            else:
                candidates = [o for o in existing.get(key, []) if str(o["id"]) not in used_ids]
                if trip and len(candidates) > 1 and day:  # prefer the one already on this day
                    on_day = [o for o in candidates
                              if any(it.get("date") == day for it in trip.itinerary_for(str(o["id"])))]
                    candidates = on_day or candidates
                obj = candidates[0] if candidates else None
            op: dict[str, Any] = {"index": index, "type": item.type, "name": item.name, "to": day or "trip_wide"}
            repeat = key in claimed and not item.id
            if obj is None and repeat:
                op.update(action="create", repeat_of=claimed_index(self.item_ops, key))
            elif obj is None:
                op.update(action="create")
            else:
                oid = str(obj["id"])
                used_ids.add(oid)
                desired = _desired(item, day, self.tz)
                changes = {}
                if not repeat:
                    for k, v in desired.items():
                        cur = _current(obj, k, day, self.tz)
                        if not _same(cur, v):
                            changes[k] = [cur, v]
                where = [it.get("date") if not it.get("is_global") else "trip_wide"
                         for it in trip.itinerary_for(oid)] if trip else []
                op.update(id=oid, action="update" if changes else "unchanged", changes=changes, scheduled_on=where)
                if (day or "trip_wide") not in where:
                    op["reschedule"] = True
            if not item.id and key not in claimed:
                claimed[key] = op.get("id", f"new:{index}")
            self.item_ops.append(op)

        if trip:
            planned = {op["id"] for op in self.item_ops if op.get("id")}
            for (t, oid), obj in trip.objects.items():
                if oid not in planned:
                    where = [it.get("date") if not it.get("is_global") else "trip_wide" for it in trip.itinerary_for(oid)]
                    self.not_in_plan.append({"type": t, "id": oid, "name": obj.get("name"), "scheduled_on": where})

    def reorder_dates(self) -> list[str | None]:
        """Groups whose current order differs from the plan's (existing items only; new ones change it anyway)."""
        if not self.trip:
            return []
        out: list[str | None] = []
        groups: list[tuple[str | None, list[Any]]] = [(None, self.plan.trip_wide)] + [
            (d.date, d.items) for d in self.plan.days]
        ops = iter(self.item_ops)
        for date, items in groups:
            planned = [next(ops) for _ in items]
            if not planned:
                continue
            if any(op["action"] == "create" or op.get("reschedule") for op in planned):
                out.append(date)
                continue
            current = [str(it["object_id"]) for it in self.trip.group(date)]
            want = [op["id"] for op in planned]
            if [c for c in current if c in want] != want:
                out.append(date)
        return out

    def summary(self, allow_deletes: bool) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for op in self.item_ops:
            counts[op["action"]] = counts.get(op["action"], 0) + 1
        moves = sum(1 for op in self.item_ops if op.get("reschedule") and op["action"] != "create")
        body = {
            "trip": self.trip_op,
            "days": self.day_ops,
            "items": [op for op in self.item_ops if op["action"] != "unchanged" or op.get("reschedule")],
            "reorder": ["trip_wide" if d is None else d for d in self.reorder_dates()],
            "not_in_plan": self.not_in_plan,
            "not_in_plan_action": "delete" if allow_deletes else "keep",
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return {
            "counts": {**counts, "schedule_changes": moves, "day_titles": len(self.day_ops),
                       "not_in_plan": len(self.not_in_plan)},
            **body,
            "diff_hash": digest,
        }


def claimed_index(ops: list[dict[str, Any]], key: tuple[str, str]) -> int | None:
    for op in ops:
        if (op["type"], norm_name(op["name"])) == key:
            return op["index"]
    return None


# ---------------------------------------------------------------------------- apply

_EXPECTED = (ApiError, PlanError, ValueError, httpx.HTTPError)


async def apply_plan(svc: TripService, plan: TripPlan, trip_id: str | None, *, dry_run: bool,
                     allow_deletes: bool, expected_diff_hash: str | None,
                     progress: Callable[[int, int, str], Awaitable[None]] | None = None) -> dict[str, Any]:
    trip = await svc.load_trip(trip_id) if trip_id else None
    diff = PlanDiff(plan, trip)
    preview = diff.summary(allow_deletes)
    if dry_run:
        return {"dry_run": True, **preview,
                "next_step": "Show the user this diff. To apply, call again with dry_run=false and "
                             f"expected_diff_hash='{preview['diff_hash']}'."}
    if expected_diff_hash and expected_diff_hash != preview["diff_hash"]:
        return {"applied": False, "reason": "The trip changed since the dry run (diff_hash differs). Review the new "
                                            "diff and apply again with its hash.", "dry_run": True, **preview}

    log: list[str] = []
    total = len(diff.item_ops) + len(diff.day_ops) + 2
    done = 0

    async def tick(msg: str) -> None:
        nonlocal done
        done += 1
        if progress:
            await progress(done, total, msg)

    # 1. trip
    if trip is None:
        f = diff.trip_op["fields"]
        trip = await svc.create_trip(f["name"], f.get("start_date"), f.get("end_date"), f.get("description"),
                                     bool(f.get("is_public")))
        log.append(f"created trip {trip.id}")
    elif diff.trip_op["action"] == "update":
        trip = await svc.update_trip(trip.id, **{k: v[1] for k, v in diff.trip_op["changes"].items()})
        log.append("updated trip fields")
    await tick("trip")
    tz = plan.timezone or trip.timezone

    # 2. day titles
    for op in diff.day_ops:
        day = next(d for d in plan.days if d.date == op["date"])
        await svc.upsert_day(trip.id, day.date, day.name, day.description)
        await tick(f"day {day.date}")

    # 3. items: writes run 2-wide (AdventureLog's default is 2 gunicorn workers); itinerary changes per
    #    group are serialised so in-memory ordering stays consistent.
    sem = asyncio.Semaphore(2)
    group_locks: dict[str | None, asyncio.Lock] = {}
    created_ids: dict[int, str | None] = {}  # plan index -> object id (None if its write failed)
    placements = diff._placements()
    errors: list[str] = []

    # every day (or "trip_wide") each object is planned on, so moving it to one day never removes another
    def identity(op: dict[str, Any]) -> str:
        if op.get("id"):
            return op["id"]
        return f"new:{op['repeat_of'] if op.get('repeat_of') is not None else op['index']}"

    planned_on: dict[str, set[str]] = {}
    for op in diff.item_ops:
        planned_on.setdefault(identity(op), set()).add(op["to"])

    async def run_op(op: dict[str, Any]) -> None:
        day, item = placements[op["index"]]
        try:
            if op["action"] == "unchanged" and not op.get("reschedule"):
                return
            keep = planned_on.get(identity(op), set())
            if op.get("repeat_of") is not None and op["action"] == "create":
                # same item planned on another day: wait for its creation, then just schedule it here
                while op["repeat_of"] not in created_ids:
                    await asyncio.sleep(0.05)
                if created_ids[op["repeat_of"]] is None:
                    raise PlanError("its first occurrence failed")
                op = {**op, "id": created_ids[op["repeat_of"]], "action": "unchanged", "reschedule": True}
            async with sem:
                lock = group_locks.setdefault(day, asyncio.Lock())
                if op["action"] in ("create", "update"):
                    async with lock:
                        res = await _upsert(svc, trip, item, op.get("id"), day, tz)
                        await svc.schedule(trip, item.type, res["id"], day, day is None, keep=keep)
                    created_ids[op["index"]] = res["id"]
                    log.append(f"{op['action']}d {item.type} {item.name!r}")
                elif op.get("reschedule"):
                    async with lock:
                        await svc.schedule(trip, item.type, op["id"], day, day is None, keep=keep)
                    log.append(f"scheduled {item.type} {item.name!r} on {day or 'trip-wide'}")
        except _EXPECTED as exc:  # keep going; report every failure at the end
            created_ids.setdefault(op["index"], None)
            errors.append(f"{item.type} {item.name!r}: {exc}")
        finally:
            await tick(item.name)

    # repeats wait on their first occurrence, so start them last
    ordered = sorted(diff.item_ops, key=lambda o: o.get("repeat_of") is not None)
    await asyncio.gather(*(run_op(op) for op in ordered))

    # 4. order each touched group as in the plan (entries not in the plan keep their place after)
    trip = await svc.load_trip(trip.id)
    oids_by_index = {op["index"]: op.get("id") or created_ids.get(op["index"]) for op in diff.item_ops}
    for op in diff.item_ops:
        if op.get("repeat_of") is not None and not oids_by_index.get(op["index"]):
            oids_by_index[op["index"]] = created_ids.get(op["repeat_of"])
    for date in dict.fromkeys([*diff.reorder_dates(), *_touched(diff)]):
        group = trip.group(date)
        want = [oids_by_index[i] for i, (d, _) in enumerate(placements) if d == date and oids_by_index.get(i)]
        ordered_entries = []
        for oid in want:
            hit = next((it for it in group if str(it["object_id"]) == str(oid) and it not in ordered_entries), None)
            if hit:
                ordered_entries.append(hit)
        ordered_entries += [it for it in group if it not in ordered_entries]
        if [it["id"] for it in ordered_entries] != [it["id"] for it in group]:
            await svc.reorder_day(trip.id, date, [str(it["id"]) for it in ordered_entries])
    await tick("ordering")

    # 5. not-in-plan
    if allow_deletes:
        for extra in diff.not_in_plan:
            try:
                if extra["type"] == "location":
                    await _remove_location_from_trip(svc, trip, extra["id"])
                    log.append(f"removed location {extra['name']!r} from the trip")
                else:
                    await svc.delete_item(extra["type"], extra["id"])
                    log.append(f"deleted {extra['type']} {extra['name']!r}")
            except _EXPECTED as exc:
                errors.append(f"remove {extra['type']} {extra['name']!r}: {exc}")

    return {"applied": True, "trip_id": trip.id, "diff_hash": preview["diff_hash"], "counts": preview["counts"],
            "errors": errors, "log": log}


def _touched(diff: PlanDiff) -> list[str | None]:
    dates = {None if op["to"] == "trip_wide" else op["to"] for op in diff.item_ops
             if op["action"] != "unchanged" or op.get("reschedule")}
    return sorted(dates, key=lambda d: d or "")


async def _remove_location_from_trip(svc: TripService, trip: Trip, location_id: str) -> None:
    """Locations can belong to several trips: take it off this trip instead of deleting it everywhere."""
    for it in trip.itinerary_for(location_id):
        await svc.unschedule(str(it["id"]), keep_visit=False)
    obj = trip.object("location", location_id) or {}
    remaining = [str(c) for c in obj.get("collections") or [] if str(c) != trip.id]
    await svc.api.patch(f"/api/locations/{location_id}/", {"collections": remaining})


async def _upsert(svc: TripService, trip: Trip, item: Any, item_id: str | None, day: str | None,
                  tz: str) -> dict[str, Any]:
    common = {"trip_id": trip.id, "trip": trip, "date": day, "trip_wide": day is None, "description": item.description,
              "auto_schedule": False}
    if day:
        _in_trip(trip, day)
    if item.type == "location":
        ltz = item.timezone or tz
        return await svc.upsert_location(
            location_id=item_id, name=item.name, category=item.category, category_icon=item.category_icon,
            latitude=item.latitude, longitude=item.longitude, address=item.address, link=item.link,
            price=item.price, price_currency=item.price_currency,
            visit_start=item.start and _on_day(item.start, day), visit_end=item.end and _on_day(item.end, day),
            timezone=ltz, **common)
    if item.type == "lodging":
        return await svc.upsert_lodging(
            lodging_id=item_id, name=item.name, lodging_type=item.lodging_type, check_in=item.check_in,
            check_out=item.check_out,
            timezone=item.timezone or (tz if item.check_in or item.check_out else None), address=item.address, latitude=item.latitude,
            longitude=item.longitude, reservation_number=item.reservation_number, price=item.price,
            price_currency=item.price_currency, link=item.link, **common)
    if item.type == "transportation":
        return await svc.upsert_transportation(
            transportation_id=item_id, name=item.name, transport_type=item.transport_type, depart=item.depart,
            arrive=item.arrive, start_timezone=item.start_timezone or (tz if item.depart or item.arrive else None),
            end_timezone=item.end_timezone,
            from_location=item.from_location, to_location=item.to_location, start_code=item.start_code,
            end_code=item.end_code, flight_number=item.flight_number, origin_latitude=item.origin_latitude,
            origin_longitude=item.origin_longitude, destination_latitude=item.destination_latitude,
            destination_longitude=item.destination_longitude, price=item.price,
            price_currency=item.price_currency, link=item.link, **common)
    common.pop("description")
    if item.type == "note":
        return await svc.upsert_note(note_id=item_id, name=item.name, content=item.content, links=item.links,
                                     **common)
    return await svc.upsert_checklist(
        checklist_id=item_id, name=item.name,
        items=None if item.items is None else [i.model_dump() for i in item.items], **common)


# ---------------------------------------------------------------------------- export


def export_plan(trip: Trip) -> dict[str, Any]:
    """Render a live trip as a TripPlan (ids included), so it can be edited and re-applied."""
    from .timeutil import to_local

    tz = trip.timezone

    def num(v: Any) -> float | None:
        try:
            return None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            return None

    def item_for(itin: dict[str, Any], day: str | None) -> dict[str, Any] | None:
        t = trip.item_type_of(itin)
        o = trip.object(t, str(itin["object_id"]))
        if o is None:
            return None
        d: dict[str, Any] = {"type": t, "id": str(o["id"]), "name": o.get("name")}
        if t in ("location", "lodging", "transportation") and o.get("description"):
            d["description"] = o["description"]
        if t == "location":
            cat = o.get("category") or {}
            d.update(category=cat.get("display_name") or cat.get("name"), category_icon=cat.get("icon"),
                     latitude=num(o.get("latitude")), longitude=num(o.get("longitude")), address=o.get("location"),
                     link=o.get("link"), price=num(o.get("price")),
                     price_currency=o.get("price_currency") if num(o.get("price")) else None)
            for v in o.get("visits") or []:
                vtz = v.get("timezone") or tz
                if day and local_date(v.get("start_date"), vtz) == day:
                    start, end = to_local(v["start_date"], vtz), to_local(v.get("end_date"), vtz)
                    d["start"] = start[11:] if start[:10] == day else start
                    if end:
                        d["end"] = end[11:] if end[:10] == day else end
                    if vtz != tz:
                        d["timezone"] = vtz
                    break
        elif t == "lodging":
            ltz = o.get("timezone") or tz
            d.update(lodging_type=o.get("type"), check_in=to_local(o.get("check_in"), ltz),
                     check_out=to_local(o.get("check_out"), ltz), timezone=ltz, address=o.get("location"),
                     latitude=num(o.get("latitude")), longitude=num(o.get("longitude")),
                     reservation_number=o.get("reservation_number"), price=num(o.get("price")),
                     price_currency=o.get("price_currency") if num(o.get("price")) else None, link=o.get("link"))
        elif t == "transportation":
            stz = o.get("start_timezone") or tz
            etz = o.get("end_timezone") or stz
            d.update(transport_type=o.get("type"), depart=to_local(o.get("date"), stz),
                     arrive=to_local(o.get("end_date"), etz), start_timezone=stz, end_timezone=etz,
                     from_location=o.get("from_location"), to_location=o.get("to_location"),
                     start_code=o.get("start_code"), end_code=o.get("end_code"),
                     flight_number=o.get("flight_number"), origin_latitude=num(o.get("origin_latitude")),
                     origin_longitude=num(o.get("origin_longitude")),
                     destination_latitude=num(o.get("destination_latitude")),
                     destination_longitude=num(o.get("destination_longitude")), price=num(o.get("price")),
                     price_currency=o.get("price_currency") if num(o.get("price")) else None, link=o.get("link"))
        elif t == "note":
            d.update(content=o.get("content"), links=o.get("links") or None)
        elif t == "checklist":
            d["items"] = [{"name": i.get("name"), "checked": bool(i.get("is_checked"))} for i in o.get("items") or []]
        return {k: v for k, v in d.items() if v not in (None, "", [])}

    dates = sorted({d["date"] for d in trip.days} | {it["date"] for it in trip.itinerary
                                                       if it.get("date") and not it.get("is_global")})
    days = []
    for date in dates:
        meta = trip.day_meta(date) or {}
        entry = {"date": date, "name": meta.get("name"), "description": meta.get("description"),
                 "items": [x for it in trip.group(date) if (x := item_for(it, date))]}
        days.append({k: v for k, v in entry.items() if v not in (None, "")})
    plan = {
        "name": trip.name, "description": trip.raw.get("description") or None, "start_date": trip.start_date,
        "end_date": trip.end_date, "is_public": trip.is_public, "timezone": tz,
        "trip_wide": [x for it in trip.group(None) if (x := item_for(it, None))],
        "days": days,
    }
    plan = {k: v for k, v in plan.items() if v is not None}
    TripPlan.model_validate(plan)  # exported plans must always be valid input
    scheduled = {str(it["object_id"]) for it in trip.itinerary}
    unscheduled = [{"type": t, "id": oid, "name": o.get("name")} for (t, oid), o in trip.objects.items()
                   if oid not in scheduled]
    return {"trip_id": trip.id, "plan": plan, "unscheduled": unscheduled}
