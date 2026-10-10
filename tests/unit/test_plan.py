from __future__ import annotations

import copy

import pytest

from adventurelog_mcp.plan import PlanDiff, TripPlan, _on_day, export_plan, norm_name
from adventurelog_mcp.service import PlanError
from adventurelog_mcp.trip import Trip

from .test_trip_and_time import NZ, _raw_trip

LOC1 = "00000000-0000-4000-8000-00000000a001"


def _plan_from_trip() -> dict:
    return export_plan(Trip(_raw_trip()))["plan"]


def test_export_reapplies_as_no_op():
    trip = Trip(_raw_trip())
    diff = PlanDiff(TripPlan.model_validate(_plan_from_trip()), trip)
    summary = diff.summary(allow_deletes=False)
    assert summary["items"] == [] and summary["reorder"] == [] and summary["days"] == []
    assert summary["trip"]["action"] == "unchanged"
    assert [x["name"] for x in summary["not_in_plan"]] == ["Unplanned"]


def test_name_matching_is_normalised_and_category_matches_display_name():
    plan = _plan_from_trip()
    gardens = plan["days"][0]["items"][1]
    gardens.pop("id")
    gardens["name"] = "  GARDENS "
    gardens["category"] = "Sightseeing"  # display name; the slug is "sightseeing"
    summary = PlanDiff(TripPlan.model_validate(plan), Trip(_raw_trip())).summary(False)
    assert summary["items"] == [] or all(op["action"] == "update" and set(op["changes"]) == {"name"}
                                         for op in summary["items"])


def test_changed_visit_time_and_move_are_detected():
    plan = _plan_from_trip()
    gardens = plan["days"][0]["items"].pop(1)
    gardens["start"], gardens["end"] = "09:00", "10:00"
    plan["days"].append({"date": "2026-12-15", "items": [gardens]})
    summary = PlanDiff(TripPlan.model_validate(plan), Trip(_raw_trip())).summary(False)
    (op,) = summary["items"]
    assert op["id"] == LOC1 and op["action"] == "update" and op["reschedule"] and op["to"] == "2026-12-15"
    assert op["changes"]["visit"][1] == ("2026-12-14T20:00:00Z", "2026-12-14T21:00:00Z")


def test_new_items_and_repeats():
    plan = _plan_from_trip()
    plan["days"].append({"date": "2026-12-15", "items": [
        {"type": "location", "name": "Tram", "start": "09:00"},
        {"type": "note", "name": "Gardens"},  # same name, different type: separate item
    ]})
    plan["days"].append({"date": "2026-12-16", "items": [{"type": "location", "name": "tram"}]})
    summary = PlanDiff(TripPlan.model_validate(plan), Trip(_raw_trip())).summary(False)
    creates = [op for op in summary["items"] if op["action"] == "create"]
    assert [(op["name"], op.get("repeat_of")) for op in creates] == [("Tram", None), ("Gardens", None),
                                                                      ("tram", creates[0]["index"])]
    assert set(summary["reorder"]) == {"2026-12-15", "2026-12-16"}


def test_diff_hash_is_stable_and_sensitive():
    trip = Trip(_raw_trip())
    plan = _plan_from_trip()
    h1 = PlanDiff(TripPlan.model_validate(plan), trip).summary(False)["diff_hash"]
    assert h1 == PlanDiff(TripPlan.model_validate(copy.deepcopy(plan)), trip).summary(False)["diff_hash"]
    plan["days"][0]["name"] = "Different"
    assert h1 != PlanDiff(TripPlan.model_validate(plan), trip).summary(False)["diff_hash"]
    assert h1 != PlanDiff(TripPlan.model_validate(_plan_from_trip()), trip).summary(True)["diff_hash"]


def test_days_outside_trip_and_unknown_ids_are_refused():
    plan = _plan_from_trip()
    plan["days"].append({"date": "2027-01-01", "items": []})
    with pytest.raises(PlanError, match="outside the trip"):
        PlanDiff(TripPlan.model_validate(plan), Trip(_raw_trip()))
    plan = _plan_from_trip()
    plan["days"][0]["items"][0]["id"] = "00000000-0000-4000-8000-0000000000ff"
    with pytest.raises(PlanError, match="not in this trip"):
        PlanDiff(TripPlan.model_validate(plan), Trip(_raw_trip()))


def test_unknown_fields_are_rejected():
    plan = _plan_from_trip()
    plan["days"][0]["items"][0]["colour"] = "red"
    with pytest.raises(ValueError):
        TripPlan.model_validate(plan)


def test_on_day_times():
    assert _on_day("9:05", "2026-12-14") == "2026-12-14T09:05"
    assert _on_day("2026-12-15T01:30", "2026-12-14") == "2026-12-15T01:30"
    with pytest.raises(PlanError):
        _on_day("09:00", None)


def test_norm_name():
    assert norm_name("  Mueller  Hut — Route ") == norm_name("mueller hut — route")
    assert norm_name("Café") == norm_name("CAFÉ")


def test_new_trip_needs_a_name():
    with pytest.raises(PlanError, match="needs a name"):
        PlanDiff(TripPlan.model_validate({"days": []}), None)
    diff = PlanDiff(TripPlan.model_validate({"name": "X", "timezone": NZ, "days": []}), None)
    assert diff.summary(False)["trip"] == {"action": "create", "fields": {"name": "X"}}
