from __future__ import annotations

import httpx
import pytest
import respx

from adventurelog_mcp.client import AdventureLogClient, ApiError
from adventurelog_mcp.server import _key_from_headers
from adventurelog_mcp.service import PlanError, TripService, _in_trip
from adventurelog_mcp.timeutil import local_date, to_local, to_utc_iso
from adventurelog_mcp.trip import Trip

NZ = "Pacific/Auckland"


def test_time_roundtrip_across_dateline():
    utc = to_utc_iso("2026-12-14T05:00", NZ)
    assert utc == "2026-12-13T16:00:00Z"
    assert to_local(utc, NZ) == "2026-12-14T05:00"
    assert local_date(utc, NZ) == "2026-12-14"
    assert to_utc_iso("2026-12-12T19:10-08:00", NZ) == "2026-12-13T03:10:00Z"  # explicit offset wins


def test_bad_timezone_is_a_value_error():
    with pytest.raises(ValueError, match="IANA"):
        to_utc_iso("2026-12-14T05:00", "NZST")


def _raw_trip() -> dict:
    cid = "c1"
    return {
        "id": cid, "name": "Test", "start_date": "2026-12-14", "end_date": "2026-12-16", "is_public": False,
        "status": "upcoming", "collaborators": [{"username": "jon", "is_owner": True}],
        "itinerary_days": [{"id": "d1", "date": "2026-12-14", "name": "Arrive", "description": None}],
        "itinerary": [
            {"id": "i2", "object_id": "loc1", "item": {"type": "location"}, "date": "2026-12-14", "is_global": False,
             "order": 2},
            {"id": "i1", "object_id": "lod1", "item": {"type": "lodging"}, "date": "2026-12-14", "is_global": False,
             "order": 1},
            {"id": "i3", "object_id": "n1", "item": {"type": "note"}, "date": None, "is_global": True, "order": 0},
        ],
        "locations": [
            {"id": "loc1", "name": "Gardens", "category": {"name": "sightseeing", "display_name": "Sightseeing"},
             "latitude": -43.5, "longitude": 172.6, "description": "x" * 500,
             "visits": [{"start_date": "2026-12-14T03:00:00Z", "end_date": "2026-12-14T04:30:00Z", "timezone": NZ}]},
            {"id": "loc2", "name": "Unplanned", "visits": []},
        ],
        "lodging": [{"id": "lod1", "name": "Hotel", "timezone": NZ, "check_in": "2026-12-14T01:00:00Z",
                     "check_out": "2026-12-15T22:00:00Z", "type": "hotel", "price": None}],
        "transportations": [],
        "notes": [{"id": "n1", "name": "Packing", "content": "boots", "links": []}],
        "checklists": [],
    }


def test_render_groups_orders_and_localises():
    trip = Trip(_raw_trip())
    assert trip.timezone == NZ
    out = trip.render()
    day = out["days"][0]
    assert day["name"] == "Arrive"
    assert [e["name"] for e in day["items"]] == ["Hotel", "Gardens"]
    hotel, gardens = day["items"]
    assert hotel["check_in"] == "2026-12-14T14:00" and hotel["check_out"] == "2026-12-16T11:00"
    assert gardens["visit"] == {"start": "2026-12-14T16:00", "end": "2026-12-14T17:30", "timezone": NZ}
    assert len(gardens["description"]) == 240 and gardens["description"].endswith("…")
    assert [e["name"] for e in out["trip_wide"]] == ["Packing"]
    assert [u["name"] for u in out["unscheduled"]] == ["Unplanned"]
    assert "price_currency" not in hotel  # empty fields dropped


def test_date_must_be_inside_trip():
    trip = Trip(_raw_trip())
    assert _in_trip(trip, "2026-12-15") == "2026-12-15"
    with pytest.raises(PlanError, match="outside the trip"):
        _in_trip(trip, "2026-12-17")


@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": "Bearer al_abc"}, "al_abc"),
    ({"authorization": "Api-Key al_def"}, "al_def"),
    ({"X-API-Key": " al_ghi "}, "al_ghi"),
    ({"Authorization": "Basic Zm9v"}, None),
    (None, None),
])
def test_key_from_headers(headers, expected):
    assert _key_from_headers(headers) == expected


@respx.mock
async def test_api_errors_are_flattened():
    respx.post("http://al/api/itineraries/").mock(
        return_value=httpx.Response(400, json={"order": ["This field is required."]}))
    client = AdventureLogClient("http://al", "al_x")
    with pytest.raises(ApiError) as exc:
        await client.post("/api/itineraries/", {})
    assert exc.value.status == 400 and exc.value.detail == "order: This field is required."


@respx.mock
async def test_schedule_moves_single_existing_entry_instead_of_duplicating():
    route = respx.post("http://al/api/itineraries/reorder/").mock(
        return_value=httpx.Response(200, json=[
            {"id": "i2", "date": "2026-12-15", "is_global": False, "order": 0}]))
    create = respx.post("http://al/api/itineraries/").mock(return_value=httpx.Response(201, json={}))
    svc = TripService(AdventureLogClient("http://al", "al_x"))
    result = await svc.schedule(Trip(_raw_trip()), "location", "loc1", "2026-12-15", False)
    assert result == {"itinerary_id": "i2", "date": "2026-12-15", "trip_wide": False, "order": 0}
    assert route.called and not create.called


@respx.mock
async def test_delete_trip_requires_matching_name():
    respx.get("http://al/api/collections/c1/").mock(return_value=httpx.Response(200, json=_raw_trip()))
    delete = respx.delete("http://al/api/collections/c1/").mock(return_value=httpx.Response(204))
    svc = TripService(AdventureLogClient("http://al", "al_x"))
    with pytest.raises(PlanError):
        await svc.delete_trip("c1", "Wrong")
    assert not delete.called
    assert (await svc.delete_trip("c1", "Test"))["deleted"]
