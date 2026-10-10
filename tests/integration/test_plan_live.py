"""apply_trip_plan / export_trip_plan against a live AdventureLog (scratch trip, removed afterwards)."""

from __future__ import annotations

import copy
import os
import uuid

import pytest
from mcp import Client

from adventurelog_mcp.server import build_server

from .test_live import Session, _day

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ADVENTURELOG_URL") and os.environ.get("ADVENTURELOG_API_KEY")),
    reason="needs a live AdventureLog (ADVENTURELOG_URL + ADVENTURELOG_API_KEY)",
)


def _plan(name: str) -> dict:
    return {
        "name": name, "start_date": "2027-03-01", "end_date": "2027-03-03", "timezone": "Europe/Lisbon",
        "description": "adventurelog-mcp plan test; safe to delete",
        "trip_wide": [{"type": "note", "name": "Emergency numbers", "content": "112"}],
        "days": [
            {"date": "2027-03-01", "name": "Arrive Lisbon", "items": [
                {"type": "transportation", "name": "TP 236 EWR → LIS", "transport_type": "plane",
                 "depart": "2027-02-28T21:30", "start_timezone": "America/New_York",
                 "arrive": "2027-03-01T09:20", "end_timezone": "Europe/Lisbon", "flight_number": "TP 236",
                 "start_code": "EWR", "end_code": "LIS"},
                {"type": "lodging", "name": "Alfama Guesthouse", "lodging_type": "bnb",
                 "check_in": "2027-03-01T15:00", "check_out": "2027-03-03T11:00",
                 "latitude": 38.7110, "longitude": -9.1300},
                {"type": "location", "name": "Miradouro de Santa Luzia", "category": "Sightseeing",
                 "latitude": 38.7118, "longitude": -9.1302, "start": "17:30", "end": "18:30"},
            ]},
            {"date": "2027-03-02", "items": [
                {"type": "location", "name": "Tram 28", "category": "Transport", "category_icon": "🚋",
                 "start": "09:00", "end": "10:00"},
                {"type": "location", "name": "Time Out Market", "category": "Food & Drink", "start": "13:00"},
                {"type": "checklist", "name": "Bookings", "items": [{"name": "Fado dinner"}]},
            ]},
            {"date": "2027-03-03", "items": [
                {"type": "location", "name": "Tram 28", "category": "Transport"},  # same place, second day
            ]},
        ],
    }


async def test_plan_create_reapply_edit():
    async with Client(build_server()) as client:
        call = Session(client)
        name = f"mcp-plan-{uuid.uuid4().hex[:8]}"
        plan = _plan(name)

        preview = await call("apply_trip_plan", plan=plan)
        assert preview["dry_run"] and preview["trip"]["action"] == "create"
        assert preview["counts"]["create"] == 8

        applied = await call("apply_trip_plan", plan=plan, dry_run=False, expected_diff_hash=preview["diff_hash"])
        tid = applied["trip_id"]
        created_location_ids: set[str] = set()
        try:
            for d in (await call("get_trip", trip_id=tid)).get("days", []):
                created_location_ids |= {e["id"] for e in d.get("items", []) if e["type"] == "location"}
            assert applied["errors"] == []
            t = await call("get_trip", trip_id=tid)
            assert t["timezone"] == "Europe/Lisbon"
            d1 = _day(t, "2027-03-01")
            assert [e["name"] for e in d1] == ["TP 236 EWR → LIS", "Alfama Guesthouse", "Miradouro de Santa Luzia"]
            assert d1[0]["depart"] == "2027-02-28T21:30" and d1[0]["start_timezone"] == "America/New_York"
            assert d1[2]["visit"]["start"] == "2027-03-01T17:30"
            assert [e["name"] for e in _day(t, "2027-03-02")] == ["Tram 28", "Time Out Market", "Bookings"]
            tram_ids = {e["id"] for d in ("2027-03-02", "2027-03-03") for e in _day(t, d) if e["name"] == "Tram 28"}
            assert len(tram_ids) == 1  # one location on two days
            assert t["days"][0]["name"] == "Arrive Lisbon"

            # idempotent: same plan again is a no-op
            again = await call("apply_trip_plan", plan=plan, trip_id=tid)
            assert again["items"] == [] and again["reorder"] == [] and again["days"] == []

            # export -> re-apply is also a no-op
            exported = await call("export_trip_plan", trip_id=tid)
            assert (await call("apply_trip_plan", plan=exported["plan"], trip_id=tid))["items"] == []

            # edit: retime + move the viewpoint to day 2 (first), drop the market, rename day 2
            edited = copy.deepcopy(plan)
            viewpoint = edited["days"][0]["items"].pop(2)
            viewpoint.update(start="08:00", end="08:45")
            edited["days"][1]["items"].insert(0, viewpoint)
            edited["days"][1]["items"] = [i for i in edited["days"][1]["items"] if i["name"] != "Time Out Market"]
            edited["days"][1]["name"] = "Trams and views"
            diff = await call("apply_trip_plan", plan=edited, trip_id=tid, allow_deletes=True)
            assert [x["name"] for x in diff["not_in_plan"]] == ["Time Out Market"]
            assert diff["days"] == [{"date": "2027-03-02", "changes": {"name": [None, "Trams and views"]}}]

            stale = await call("apply_trip_plan", plan=edited, trip_id=tid, dry_run=False, allow_deletes=True,
                               expected_diff_hash="0000000000000000")
            assert stale["applied"] is False

            done = await call("apply_trip_plan", plan=edited, trip_id=tid, dry_run=False, allow_deletes=True,
                              expected_diff_hash=diff["diff_hash"])
            assert done["errors"] == []
            t = await call("get_trip", trip_id=tid)
            assert [e["name"] for e in _day(t, "2027-03-01")] == ["TP 236 EWR → LIS", "Alfama Guesthouse"]
            d2 = _day(t, "2027-03-02")
            assert [e["name"] for e in d2] == ["Miradouro de Santa Luzia", "Tram 28", "Bookings"]
            assert d2[0]["visit"] == {"start": "2027-03-02T08:00", "end": "2027-03-02T08:45",
                                      "timezone": "Europe/Lisbon"}
            assert "Time Out Market" not in [u["name"] for u in t.get("unscheduled", [])]
            assert (await call("apply_trip_plan", plan=edited, trip_id=tid))["items"] == []
        finally:
            # Delete ONLY locations this test created. Never discover ids via search: it is fuzzy and
            # once matched (and deleted) real locations in other trips.
            for lid in created_location_ids:
                await call("delete_item", item_type="location", item_id=lid)
            await call("delete_trip", trip_id=tid, confirm_name=name)
