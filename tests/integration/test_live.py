"""End-to-end test against a real AdventureLog instance.

Creates a throwaway trip, exercises every write tool through the MCP layer and
deletes the trip afterwards. Runs only when ADVENTURELOG_URL and
ADVENTURELOG_API_KEY are set (CI starts a disposable AdventureLog for it).
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from mcp import Client

from adventurelog_mcp.server import build_server

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ADVENTURELOG_URL") and os.environ.get("ADVENTURELOG_API_KEY")),
    reason="needs a live AdventureLog (ADVENTURELOG_URL + ADVENTURELOG_API_KEY)",
)

TZ = "Pacific/Auckland"


def _out(result):
    assert not result.is_error, result.content[0].text if result.content else result
    sc = result.structured_content
    if sc is not None and set(sc) == {"result"}:
        return sc["result"]
    return json.loads(result.content[0].text)


class Session:
    def __init__(self, client: Client):
        self.c = client

    async def __call__(self, tool: str, **args):
        return _out(await self.c.call_tool(tool, args))

    async def error(self, tool: str, **args) -> str:
        r = await self.c.call_tool(tool, args)
        assert r.is_error, f"{tool} should have failed"
        return r.content[0].text


def _day(trip: dict, date: str) -> list[dict]:
    return next((d.get("items", []) for d in trip.get("days", []) if d["date"] == date), [])


async def test_full_planning_flow():
    async with Client(build_server()) as client:
        call = Session(client)
        name = f"mcp-it-{uuid.uuid4().hex[:8]}"
        trip = await call("create_trip", name=name, start_date="2027-01-04", end_date="2027-01-08",
                          description="adventurelog-mcp integration test; safe to delete")
        tid = trip["id"]
        gardens = market = None
        try:
            day = await call("upsert_day", trip_id=tid, date="2027-01-04", name="Arrive")
            again = await call("upsert_day", trip_id=tid, date="2027-01-04", description="Land and nap")
            assert again["day_id"] == day["day_id"] and again["name"] == "Arrive"

            flight = await call("upsert_transportation", trip_id=tid, name="NZ 3 LAX → AKL", transport_type="plane",
                                depart="2027-01-02T19:10", start_timezone="America/Los_Angeles",
                                arrive="2027-01-04T05:00", end_timezone=TZ, start_code="LAX", end_code="AKL",
                                flight_number="NZ 3", date="2027-01-04")
            assert flight["scheduled"]["date"] == "2027-01-04"

            lodging = await call("upsert_lodging", trip_id=tid, name="Test Hotel", lodging_type="hotel",
                                 check_in="2027-01-04T14:00", check_out="2027-01-06T10:00", timezone=TZ,
                                 latitude=-43.5246, longitude=172.6346, reservation_number="ABC123")
            assert lodging["scheduled"]["date"] == "2027-01-04"  # auto: local check-in date

            gardens = await call("upsert_location", trip_id=tid, name="Botanic Gardens", category="Sightseeing",
                                 latitude=-43.5306, longitude=172.6206, visit_start="2027-01-04T16:00",
                                 visit_end="2027-01-04T17:30", timezone=TZ)
            market = await call("upsert_location", trip_id=tid, name="Riverside Market", category="Food & Drink",
                                category_icon="🍽️", latitude=-43.5335, longitude=172.6343,
                                visit_start="2027-01-04T18:15", visit_end="2027-01-04T19:30")
            note = await call("upsert_note", trip_id=tid, name="Packing", content="- boots", trip_wide=True)
            todo = await call("upsert_checklist", trip_id=tid, name="To book",
                              items=[{"name": "Punting"}, {"name": "Spa", "checked": True}], date="2027-01-05")

            t = await call("get_trip", trip_id=tid)
            assert t["timezone"] == TZ
            d1 = _day(t, "2027-01-04")
            assert [e["name"] for e in d1] == ["NZ 3 LAX → AKL", "Test Hotel", "Botanic Gardens", "Riverside Market"]
            assert d1[0]["depart"] == "2027-01-02T19:10" and d1[0]["arrive"] == "2027-01-04T05:00"
            assert d1[2]["visit"] == {"start": "2027-01-04T16:00", "end": "2027-01-04T17:30", "timezone": TZ}
            assert d1[3]["category"] == "Food & Drink"
            assert [e["name"] for e in t["trip_wide"]] == ["Packing"]
            assert _day(t, "2027-01-05")[0]["items"][1] == {"id": _day(t, "2027-01-05")[0]["items"][1]["id"],
                                                             "name": "Spa", "checked": True}

            # reorder: market before gardens
            ids = [e["itinerary_id"] for e in d1]
            await call("reorder_day", trip_id=tid, date="2027-01-04", itinerary_ids=[ids[0], ids[1], ids[3], ids[2]])
            assert "must list every entry" in await call.error("reorder_day", trip_id=tid, date="2027-01-04",
                                                            itinerary_ids=ids[:2])

            # move the gardens to the 5th, first slot: visit time moves with it
            moved = await call("move_item", itinerary_id=ids[2], date="2027-01-05", position=0)
            assert moved["date"] == "2027-01-05" and moved["times_shifted_days"] == 1
            t = await call("get_trip", trip_id=tid)
            assert [e["name"] for e in _day(t, "2027-01-04")] == ["NZ 3 LAX → AKL", "Test Hotel", "Riverside Market"]
            d2 = _day(t, "2027-01-05")
            assert [e["name"] for e in d2] == ["Botanic Gardens", "To book"]
            assert d2[0]["visit"]["start"] == "2027-01-05T16:00"

            # edit in place: rename + new visit time, still one itinerary entry
            await call("upsert_location", location_id=gardens["id"], trip_id=tid, name="Christchurch Botanic Gardens",
                       visit_start="2027-01-05T09:00", visit_end="2027-01-05T10:00")
            gardens_full = await call("get_item", item_type="location", item_id=gardens["id"])
            assert len(gardens_full["visits"]) == 1
            t = await call("get_trip", trip_id=tid)
            d2 = _day(t, "2027-01-05")
            assert d2[0]["name"] == "Christchurch Botanic Gardens" and d2[0]["visit"]["start"] == "2027-01-05T09:00"

            # checklist edit replaces the item list (upstream recreates items, so ids are not stable)
            first = next(i for i in d2[1]["items"] if i["name"] == "Punting")
            await call("upsert_checklist", checklist_id=todo["id"], items=[{"id": first["id"], "name": "Punting",
                                                                           "checked": True}])
            t = await call("get_trip", trip_id=tid)
            assert [(i["name"], i["checked"]) for i in _day(t, "2027-01-05")[1]["items"]] == [("Punting", True)]

            # unschedule keeps the item, schedule puts it back
            market_entry = next(e for e in _day(t, "2027-01-04") if e["id"] == market["id"])
            await call("unschedule_item", itinerary_id=market_entry["itinerary_id"])
            t = await call("get_trip", trip_id=tid)
            assert market["id"] in [u["id"] for u in t["unscheduled"]]
            back = await call("schedule_item", trip_id=tid, item_type="location", item_id=market["id"],
                              date="2027-01-06")
            assert back["date"] == "2027-01-06"

            # delete an item
            await call("delete_item", item_type="note", item_id=note["id"])
            t = await call("get_trip", trip_id=tid)
            assert "trip_wide" not in t

            # out-of-range date is reported, not crashed
            err = await call.error("schedule_item", trip_id=tid, item_type="location", item_id=market["id"],
                                   date="2027-02-01")
            assert "is outside the trip" in err

            assert "does not match" in await call.error("delete_trip", trip_id=tid, confirm_name="nope")
        finally:
            # remove only the locations this test created (they are not owned by the collection)
            for lid in [x["id"] for x in (gardens, market) if x]:
                await call("delete_item", item_type="location", item_id=lid)
            await call("delete_trip", trip_id=tid, confirm_name=name)
