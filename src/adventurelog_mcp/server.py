"""MCP server exposing AdventureLog trip planning tools."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .client import AdventureLogClient, ApiError
from .plan import TripPlan, apply_plan, export_plan
from .service import PlanError, TripService

T = TypeVar("T")

INSTRUCTIONS = """\
Tools for planning trips in AdventureLog, a self-hosted travel planner.

A trip is an AdventureLog *collection*. It holds locations (places to visit), lodging,
transportation, notes and checklists. The *itinerary* puts those items on days
(or marks them trip-wide), in order.

Workflow:
1. list_trips, then get_trip. get_trip returns every day with its ordered entries, trip-wide
   entries and unscheduled items. Each entry has an `itinerary_id` (its place on the itinerary)
   and an `id` (the underlying item).
2. Use search_places to find real coordinates. Don't guess latitude/longitude.
3. Create or edit items with the upsert_* tools. Pass trip_id plus `date` (or trip_wide) to put the
   item on the itinerary in the same call. Lodging and transportation are scheduled on their
   local start date automatically.
4. Rearrange with move_item / reorder_day. Take items off a day with unschedule_item, and
   delete them for good with delete_item.
5. Photos: add_photos attaches real image URLs (or local files) to a location, lodging or
   transportation; get_item lists an item's photos with their ids. remove_photo,
   set_primary_photo and set_trip_cover take those ids.

Times are local wall-clock times ('2026-12-15T10:30') in the given IANA timezone. If you leave
the timezone out, the trip's timezone from get_trip is used. Re-read the trip with get_trip after
a batch of edits and before telling the user what changed.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                   open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                              open_world_hint=False)

ItemType = Literal["location", "lodging", "transportation", "note", "checklist"]
PhotoItemType = Literal["location", "lodging", "transportation"]

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_PHOTO_BYTES = 25 * 1024 * 1024

TripId = Annotated[str, Field(description="Trip (collection) UUID from list_trips.")]
OptTripId = Annotated[str | None, Field(description="Trip UUID. Required to put the item on the itinerary.")]
Date = Annotated[str | None, Field(description="Itinerary day YYYY-MM-DD (must fall within the trip dates).")]
TripWide = Annotated[bool, Field(description="Show as a trip-wide entry instead of on a specific day.")]
LocalTime = Annotated[str | None, Field(description="Local time 'YYYY-MM-DDTHH:MM'.")]
Tz = Annotated[str | None, Field(description="IANA timezone, e.g. 'Pacific/Auckland'. Defaults to the trip's.")]


class ChecklistItemIn(BaseModel):
    name: str
    checked: bool = False
    id: str | None = Field(default=None, description="Existing item id (optional).")


class Settings:
    def __init__(self) -> None:
        self.base_url = os.environ.get("ADVENTURELOG_URL", "http://localhost:8016")
        self.api_key = os.environ.get("ADVENTURELOG_API_KEY", "")
        self.read_only = os.environ.get("ADVENTURELOG_READ_ONLY", "").lower() in ("1", "true", "yes")
        # Over HTTP the caller's own key must come in a header; a server-side key would let anyone
        # who reaches /mcp act as that user. Opt in explicitly for single-user, network-isolated setups.
        self.allow_env_key_over_http = os.environ.get("ADVENTURELOG_ALLOW_ENV_KEY_OVER_HTTP", "").lower() in (
            "1", "true", "yes")
        self.http = False


def _key_from_headers(headers: Any) -> str | None:
    if not headers:
        return None
    lower = {k.lower(): v for k, v in dict(headers).items()}
    if lower.get("x-api-key"):
        return lower["x-api-key"].strip()
    auth = lower.get("authorization", "")
    for scheme in ("bearer ", "api-key "):
        if auth.lower().startswith(scheme):
            return auth[len(scheme):].strip()
    return None


def build_server(settings: Settings | None = None, *, transport: Any = None) -> MCPServer:
    """Build the server. `transport` is an httpx transport override, used by tests."""
    settings = settings or Settings()
    clients: dict[str, AdventureLogClient] = {}

    def service(ctx: Context | None) -> TripService:
        key = _key_from_headers(ctx.headers if ctx else None) if settings.http else None
        if not key:
            if settings.http and not settings.allow_env_key_over_http:
                raise ToolError(
                    "No AdventureLog API key. Send 'Authorization: Bearer al_…' (or X-API-Key) "
                    "with your personal key from AdventureLog Settings -> Security."
                )
            key = settings.api_key
        if not key:
            raise ToolError("ADVENTURELOG_API_KEY is not set.")
        cache_key = hashlib.sha256(key.encode()).hexdigest()
        if cache_key not in clients:
            if len(clients) > 64:
                clients.clear()
            clients[cache_key] = AdventureLogClient(settings.base_url, key, transport=transport)
        return TripService(clients[cache_key])

    async def run(coro: Awaitable[T]) -> T:
        try:
            return await coro
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        except ApiError as exc:
            hint = ""
            if exc.status in (401, 403):
                hint = " (check the API key and that you own or collaborate on this trip)"
            elif exc.status == 404:
                hint = " (wrong id? ids come from get_trip / list_trips)"
            raise ToolError(f"AdventureLog rejected the request: HTTP {exc.status}: {exc.detail}{hint}") from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    mcp = MCPServer(
        name="adventurelog",
        title="AdventureLog",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/jw-dickson/adventurelog-mcp",
    )

    # ------------------------------------------------------------------ read tools

    @mcp.tool(annotations=READ)
    async def list_trips(ctx: Context, include_shared: bool = True, include_archived: bool = False) -> list[dict]:
        """List the user's trips (collections), soonest first, including trips shared with them."""
        return await run(service(ctx).list_trips(include_shared, include_archived))

    @mcp.tool(annotations=READ)
    async def get_trip(ctx: Context, trip_id: TripId,
                       from_date: Annotated[str | None, Field(description="Only days on/after YYYY-MM-DD")] = None,
                       to_date: Annotated[str | None, Field(description="Only days on/before YYYY-MM-DD")] = None
                       ) -> dict:
        """Get a whole trip in one call: days (name, description, ordered entries), trip-wide entries,
        unscheduled items, collaborators and the trip's timezone. Long text is clipped; use get_item for full text.
        For long trips, pass from_date/to_date to fetch just the days you are working on."""
        return await run(_render(service(ctx), trip_id, from_date, to_date))

    @mcp.tool(annotations=READ)
    async def export_trip_plan(ctx: Context, trip_id: TripId) -> dict:
        """Export a trip as an editable plan (the same format apply_trip_plan takes, with item ids and full text).
        Edit it and pass it back to apply_trip_plan to make many changes at once."""
        svc = service(ctx)

        async def go() -> dict:
            return export_plan(await svc.load_trip(trip_id))

        return await run(go())

    @mcp.tool(annotations=READ)
    async def get_item(ctx: Context, item_type: ItemType, item_id: str) -> dict:
        """Get one item in full (untruncated description, visits, all fields). Datetimes are UTC."""
        return await run(service(ctx).get_item(item_type, item_id))

    @mcp.tool(annotations=READ)
    async def search_places(ctx: Context, query: Annotated[str, Field(description="Place name, optionally with town/region")],
                            limit: int = 5) -> list[dict]:
        """Geocode a place name to real coordinates and address. Use this before creating a location or lodging."""
        return await run(service(ctx).search_places(query, limit))

    @mcp.tool(annotations=READ)
    async def search(ctx: Context, query: str) -> list[dict]:
        """Full-text search across the user's own AdventureLog items (locations, lodging, notes, trips…)."""
        return await run(service(ctx).search(query))

    @mcp.prompt(name="plan-trip", title="Plan or revise a trip in AdventureLog")
    def plan_trip(request: str, trip_name: str | None = None) -> str:
        """Start a planning session: read the trip, propose changes, dry-run, confirm, apply."""
        target = f'the trip "{trip_name}"' if trip_name else "the right trip (or a new one)"
        return (
            f"Help me plan {target} in AdventureLog. What I want: {request}\n\n"
            "1. Find the trip with list_trips and read it with get_trip (use from_date/to_date for long trips).\n"
            "2. Propose the changes in plain language and wait for my OK.\n"
            "3. For a few edits use the upsert_*/move_item tools. For many, use export_trip_plan, edit the plan, "
            "run apply_trip_plan with dry_run=true, show me the diff, then apply with expected_diff_hash.\n"
            "4. Use search_places for coordinates; never guess them. Use local times in the trip's timezone.\n"
            "5. Never delete anything I didn't ask to delete. Finish by re-reading the trip and summarising what changed."
        )

    if settings.read_only:
        return mcp

    # ------------------------------------------------------------------ trips + days

    @mcp.tool(annotations=WRITE)
    async def create_trip(ctx: Context, name: str, start_date: Date = None, end_date: Date = None,
                          description: str | None = None, is_public: bool = False) -> dict:
        """Create a new trip. Set start/end dates so items can be scheduled onto days."""
        svc = service(ctx)
        trip = await run(svc.create_trip(name, start_date, end_date, description, is_public))
        return trip.summary()

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def update_trip(ctx: Context, trip_id: TripId, name: str | None = None, description: str | None = None,
                          start_date: Date = None, end_date: Date = None, is_public: bool | None = None) -> dict:
        """Edit trip name, description, dates or public visibility. Only the fields you pass change."""
        trip = await run(service(ctx).update_trip(trip_id, name=name, description=description,
                                                  start_date=start_date, end_date=end_date, is_public=is_public))
        return trip.summary()

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_trip(ctx: Context, trip_id: TripId,
                          confirm_name: Annotated[str, Field(description="The trip's exact name, as a safety check.")]
                          ) -> dict:
        """Permanently delete a trip. Only do this when the user explicitly asks to."""
        return await run(service(ctx).delete_trip(trip_id, confirm_name))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def upsert_day(ctx: Context, trip_id: TripId, date: Annotated[str, Field(description="YYYY-MM-DD")],
                         name: str | None = None, description: str | None = None) -> dict:
        """Set a day's title and summary (e.g. 'Tekapo → Aoraki' / 'Late start after stargazing')."""
        return await run(service(ctx).upsert_day(trip_id, date, name, description))

    # ------------------------------------------------------------------ elements

    @mcp.tool(annotations=WRITE)
    async def upsert_location(
        ctx: Context,
        trip_id: OptTripId = None,
        location_id: Annotated[str | None, Field(description="Omit to create; pass to edit.")] = None,
        name: str | None = None,
        description: str | None = None,
        category: Annotated[str | None, Field(description="e.g. Hiking, Food & Drink. Created if new.")] = None,
        category_icon: Annotated[str | None, Field(description="Emoji for a new category")] = None,
        latitude: float | None = None,
        longitude: float | None = None,
        address: str | None = None,
        link: str | None = None,
        price: float | None = None,
        price_currency: str | None = None,
        visit_start: LocalTime = None,
        visit_end: LocalTime = None,
        timezone: Tz = None,
        date: Date = None,
        trip_wide: TripWide = False,
    ) -> dict:
        """Create or edit a place to visit (sight, hike, restaurant, activity). With visit_start, a planned
        visit is recorded and the location is scheduled on that day. Fields you omit are left unchanged."""
        return await run(service(ctx).upsert_location(
            trip_id=trip_id, location_id=location_id, name=name, description=description, category=category,
            category_icon=category_icon, latitude=latitude, longitude=longitude, address=address, link=link,
            price=price, price_currency=price_currency, visit_start=visit_start, visit_end=visit_end,
            timezone=timezone, date=date, trip_wide=trip_wide))

    @mcp.tool(annotations=WRITE)
    async def upsert_lodging(
        ctx: Context,
        trip_id: OptTripId = None,
        lodging_id: Annotated[str | None, Field(description="Omit to create; pass to edit.")] = None,
        name: str | None = None,
        lodging_type: Annotated[str | None, Field(
            description="hotel, hostel, resort, bnb, campground, cabin, apartment, house, villa, motel, other")] = None,
        check_in: LocalTime = None,
        check_out: LocalTime = None,
        timezone: Tz = None,
        address: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        reservation_number: str | None = None,
        price: float | None = None,
        price_currency: str | None = None,
        link: str | None = None,
        description: str | None = None,
        date: Date = None,
        trip_wide: TripWide = False,
    ) -> dict:
        """Create or edit a place to stay. With trip_id, it is scheduled on its check-in day unless date/trip_wide is given."""
        return await run(service(ctx).upsert_lodging(
            trip_id=trip_id, lodging_id=lodging_id, name=name, lodging_type=lodging_type, check_in=check_in,
            check_out=check_out, timezone=timezone, address=address, latitude=latitude, longitude=longitude,
            reservation_number=reservation_number, price=price, price_currency=price_currency, link=link,
            description=description, date=date, trip_wide=trip_wide))

    @mcp.tool(annotations=WRITE)
    async def upsert_transportation(
        ctx: Context,
        trip_id: OptTripId = None,
        transportation_id: Annotated[str | None, Field(description="Omit to create; pass to edit.")] = None,
        name: str | None = None,
        transport_type: Annotated[str | None, Field(
            description="car, plane, train, bus, boat, bike, walking, other")] = None,
        depart: LocalTime = None,
        arrive: LocalTime = None,
        start_timezone: Tz = None,
        end_timezone: Annotated[str | None, Field(description="Arrival IANA timezone; defaults to start_timezone.")] = None,
        from_location: str | None = None,
        to_location: str | None = None,
        start_code: Annotated[str | None, Field(description="Origin airport/station code, e.g. PDX")] = None,
        end_code: str | None = None,
        flight_number: str | None = None,
        origin_latitude: float | None = None,
        origin_longitude: float | None = None,
        destination_latitude: float | None = None,
        destination_longitude: float | None = None,
        price: float | None = None,
        price_currency: str | None = None,
        link: str | None = None,
        description: str | None = None,
        date: Date = None,
        trip_wide: TripWide = False,
    ) -> dict:
        """Create or edit a journey leg (flight, drive, ferry, train). Depart/arrive are local times in their own
        timezones. With trip_id, it is scheduled on its departure day unless date/trip_wide is given."""
        return await run(service(ctx).upsert_transportation(
            trip_id=trip_id, transportation_id=transportation_id, name=name, transport_type=transport_type,
            depart=depart, arrive=arrive, start_timezone=start_timezone, end_timezone=end_timezone,
            from_location=from_location, to_location=to_location, start_code=start_code, end_code=end_code,
            flight_number=flight_number, origin_latitude=origin_latitude, origin_longitude=origin_longitude,
            destination_latitude=destination_latitude, destination_longitude=destination_longitude,
            price=price, price_currency=price_currency, link=link, description=description, date=date,
            trip_wide=trip_wide))

    @mcp.tool(annotations=WRITE)
    async def upsert_note(ctx: Context, trip_id: OptTripId = None,
                          note_id: Annotated[str | None, Field(description="Omit to create; pass to edit.")] = None,
                          name: str | None = None, content: Annotated[str | None, Field(description="Markdown")] = None,
                          links: list[str] | None = None, date: Date = None, trip_wide: TripWide = False) -> dict:
        """Create or edit a note (booking details, tips, open questions), optionally pinned to a day or trip-wide."""
        return await run(service(ctx).upsert_note(trip_id=trip_id, note_id=note_id, name=name, content=content,
                                                  links=links, date=date, trip_wide=trip_wide))

    @mcp.tool(annotations=WRITE)
    async def upsert_checklist(ctx: Context, trip_id: OptTripId = None,
                               checklist_id: Annotated[str | None, Field(description="Omit to create; pass to edit.")] = None,
                               name: str | None = None,
                               items: Annotated[list[ChecklistItemIn] | None, Field(
                                   description="The FULL list of items. On edit this replaces the whole list "
                                               "(items not listed are deleted; item ids change).")] = None,
                               date: Date = None, trip_wide: TripWide = False) -> dict:
        """Create or edit a checklist (packing list, to-book list)."""
        return await run(service(ctx).upsert_checklist(
            trip_id=trip_id, checklist_id=checklist_id, name=name,
            items=None if items is None else [i.model_dump() for i in items], date=date, trip_wide=trip_wide))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_item(ctx: Context, item_type: ItemType, item_id: str) -> dict:
        """Permanently delete an item (and its itinerary entries). A location is deleted from ALL trips it is in;
        to just take something off a day, use unschedule_item instead."""
        return await run(service(ctx).delete_item(item_type, item_id))

    # ------------------------------------------------------------------ itinerary

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def schedule_item(ctx: Context, trip_id: TripId, item_type: ItemType, item_id: str,
                            date: Date = None, trip_wide: TripWide = False) -> dict:
        """Put an existing item on a day (or trip-wide). If it is already on exactly one other day it is moved;
        use move_item to also choose its position."""
        svc = service(ctx)

        async def go() -> dict:
            if not date and not trip_wide:
                raise PlanError("Give date or trip_wide.")
            return await svc.schedule(await svc.load_trip(trip_id), item_type, item_id, date, trip_wide)

        return await run(go())

    @mcp.tool(annotations=WRITE)
    async def move_item(ctx: Context, itinerary_id: Annotated[str, Field(description="itinerary_id from get_trip")],
                        date: Date = None, trip_wide: TripWide = False,
                        position: Annotated[int | None, Field(description="0-based slot in the target day; default last")] = None,
                        shift_times: Annotated[bool, Field(
                            description="When changing days, shift the item's own times by the same number of days "
                                        "(flight/stay dates, visit time, note date).")] = True) -> dict:
        """Move an itinerary entry to another day/position, or to trip-wide."""
        return await run(service(ctx).move_item(itinerary_id, date, trip_wide, position, shift_times))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def reorder_day(ctx: Context, trip_id: TripId,
                          date: Annotated[str | None, Field(description="YYYY-MM-DD, or omit for trip-wide entries")],
                          itinerary_ids: Annotated[list[str], Field(description="Every itinerary_id on that day, in the new order")]
                          ) -> list[dict]:
        """Set the order of all entries on one day."""
        return await run(service(ctx).reorder_day(trip_id, date, itinerary_ids))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def unschedule_item(ctx: Context, itinerary_id: str,
                              keep_visit: Annotated[bool, Field(
                                  description="For locations: keep the planned visit record for that day")] = False
                              ) -> dict:
        """Take an entry off the itinerary. The item itself stays in the trip (shown as unscheduled)."""
        return await run(service(ctx).unschedule(itinerary_id, keep_visit))

    # ------------------------------------------------------------------ bulk

    @mcp.tool(annotations=DESTRUCTIVE)
    async def apply_trip_plan(
        ctx: Context,
        plan: TripPlan,
        trip_id: Annotated[str | None, Field(description="Existing trip to update; omit to create a new trip.")] = None,
        dry_run: Annotated[bool, Field(description="Default true: only show what would change.")] = True,
        expected_diff_hash: Annotated[str | None, Field(
            description="diff_hash from the dry run; the apply aborts if the trip changed since.")] = None,
        allow_deletes: Annotated[bool, Field(
            description="Remove items that are in the trip but not in the plan (locations are only taken off this "
                        "trip, never deleted from other trips). Default false keeps them.")] = False,
    ) -> dict:
        """Create or update a whole trip from a declarative plan: trip fields, day titles, and each day's items in order.

        Items match existing ones by `id`, or else by type + name, so re-applying the same plan changes nothing.
        Only the fields the plan states are written. ALWAYS run with dry_run=true first, show the user the diff,
        then apply with dry_run=false and expected_diff_hash. For small edits use the single-item tools instead."""
        svc = service(ctx)

        async def progress(done: int, total: int, msg: str) -> None:
            try:
                await ctx.report_progress(done, total, msg)
            except Exception:  # progress is best-effort
                logging.getLogger(__name__).debug("progress notification failed", exc_info=True)

        return await run(apply_plan(svc, plan, trip_id and trip_id.strip(), dry_run=dry_run,
                                    allow_deletes=allow_deletes, expected_diff_hash=expected_diff_hash,
                                    progress=progress))

    # ------------------------------------------------------------------ photos

    @mcp.tool(annotations=WRITE)
    async def add_photos(
        ctx: Context,
        item_type: PhotoItemType,
        item_id: str,
        urls: Annotated[list[str] | None, Field(
            description="Public image URLs (e.g. Wikimedia Commons, the venue's site). AdventureLog downloads them.")
        ] = None,
        file_paths: Annotated[list[str] | None, Field(
            description="Local image files to upload. Only when this server runs locally (stdio).")] = None,
        make_primary: Annotated[bool, Field(
            description="Make the first added photo the item's primary photo. An item's first photo is "
                        "primary anyway.")] = False,
    ) -> dict:
        """Add up to 10 photos to a location, lodging or transportation. Use real photos of the actual place; never
        invent URLs. Returns the new photo ids (for remove_photo / set_primary_photo / set_trip_cover) and any
        URLs that could not be fetched."""
        svc = service(ctx)

        async def go() -> dict:
            return await svc.add_photos(item_type, item_id, urls, _read_photos(file_paths, settings), make_primary)

        return await run(go())

    @mcp.tool(annotations=DESTRUCTIVE)
    async def remove_photo(ctx: Context, photo_id: Annotated[str, Field(
            description="Photo id from get_item's `photos` or add_photos")]) -> dict:
        """Permanently delete one photo from its item."""
        return await run(service(ctx).remove_photo(photo_id))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def set_primary_photo(ctx: Context, photo_id: str) -> dict:
        """Make a photo the primary (thumbnail) photo of its item."""
        return await run(service(ctx).set_primary_photo(photo_id))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def set_trip_cover(ctx: Context, trip_id: TripId,
                             photo_id: Annotated[str | None, Field(
                                 description="A photo of one of this trip's locations (that you own); "
                                             "omit to clear the cover.")] = None) -> dict:
        """Set or clear the trip's cover photo."""
        return await run(service(ctx).set_trip_cover(trip_id, photo_id))

    # ------------------------------------------------------------------ sharing

    @mcp.tool(annotations=WRITE)
    async def share_trip(ctx: Context, trip_id: TripId, username: str) -> dict:
        """Invite another AdventureLog user (who must have a public profile) to collaborate on a trip."""
        return await run(service(ctx).share_trip(trip_id, username))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def unshare_trip(ctx: Context, trip_id: TripId, username: str) -> dict:
        """Remove a collaborator from a trip."""
        return await run(service(ctx).unshare_trip(trip_id, username))

    return mcp


def _read_photos(paths: list[str] | None, settings: Settings) -> list[tuple[str, bytes]]:
    """Read local photo files for upload. Refused over HTTP: there the paths would be on the server, not the user's."""
    if not paths:
        return []
    if settings.http:
        raise ToolError("file_paths only works when the server runs locally (stdio); give photo URLs instead.")
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.suffix.lower() not in PHOTO_SUFFIXES:
            raise ToolError(f"{raw!r} is not a photo ({', '.join(PHOTO_SUFFIXES)}).")
        if not path.is_file():
            raise ToolError(f"No such file: {raw!r}")
        if path.stat().st_size > MAX_PHOTO_BYTES:
            raise ToolError(f"{raw!r} is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
        out.append((path.name, path.read_bytes()))
    return out


async def _render(svc: TripService, trip_id: str, from_date: str | None = None, to_date: str | None = None) -> dict:
    out = (await svc.load_trip(trip_id)).render()
    if from_date or to_date:
        lo, hi = from_date or "0000-00-00", to_date or "9999-99-99"
        out["days"] = [d for d in out.get("days", []) if lo <= d["date"] <= hi]
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="adventurelog-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")))
    args = parser.parse_args(argv)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if args.transport == "http":
        settings.http = True
        build_server(settings).run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
