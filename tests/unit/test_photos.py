from __future__ import annotations

import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from adventurelog_mcp.client import AdventureLogClient
from adventurelog_mcp.server import Settings, _read_photos
from adventurelog_mcp.service import PlanError, TripService
from adventurelog_mcp.trip import Trip, photo

BASE = "http://al.test"
LOC = "00000000-0000-4000-8000-00000000a001"
IMG = "00000000-0000-4000-8000-0000000001a1"


def _img(i: str = IMG, primary: bool = True) -> dict:
    return {"id": i, "image": f"{BASE}/media/images/{i}.webp", "is_primary": primary, "source": "url",
            "source_url": "https://example.org/a.jpg", "user": 1, "immich_id": None}


def _svc() -> TripService:
    return TripService(AdventureLogClient(BASE, "al_test"))


def test_photo_view_is_compact():
    assert photo(_img()) == {"id": IMG, "primary": True, "url": f"{BASE}/media/images/{IMG}.webp",
                             "source": "url", "source_url": "https://example.org/a.jpg"}


def test_trip_shows_photo_counts_and_cover():
    trip = Trip({"id": "00000000-0000-4000-8000-0000000000c1", "name": "T", "primary_image": _img(),
                 "locations": [{"id": LOC, "name": "Gardens", "images": [_img(), None]}],
                 "notes": [{"id": "00000000-0000-4000-8000-00000000d001", "name": "N"}]})
    out = trip.render()
    assert out["cover_photo"]["id"] == IMG
    by_name = {u["name"]: u for u in out["unscheduled"]}
    assert by_name["Gardens"]["photos"] == 1  # null images (Immich without integration) are not counted
    assert "photos" not in by_name["N"]


async def test_add_photos_validates_before_calling_the_api():
    svc = _svc()
    with pytest.raises(PlanError, match="location, lodging, transportation"):
        await svc.add_photos("note", LOC, ["https://example.org/a.jpg"], None, False)
    with pytest.raises(PlanError, match="at least one"):
        await svc.add_photos("location", LOC, [" "], None, False)
    with pytest.raises(PlanError, match="At most 10"):
        await svc.add_photos("location", LOC, [f"https://example.org/{n}.jpg" for n in range(11)], None, False)
    with pytest.raises(PlanError, match="http"):
        await svc.add_photos("location", LOC, ["file:///etc/passwd"], None, False)


@respx.mock
async def test_add_photos_reports_failed_urls_and_sets_primary():
    new = "00000000-0000-4000-8000-0000000001a2"
    route = respx.post(f"{BASE}/api/images/import_from_urls/").mock(return_value=httpx.Response(200, json={
        "created": [_img(new, primary=False)],
        "results": [{"url": "https://example.org/a.jpg", "status": "created", "id": new},
                    {"url": "https://example.org/b.jpg", "status": "failed", "error": "Download timeout"}]}))
    primary = respx.post(f"{BASE}/api/images/{new}/toggle_primary/").mock(
        return_value=httpx.Response(200, json={"success": "Image set as primary image"}))
    out = await _svc().add_photos("location", LOC, ["https://example.org/a.jpg", "https://example.org/b.jpg"],
                                  None, True)
    assert json.loads(route.calls[0].request.content) == {
        "content_type": "location", "object_id": LOC,
        "urls": ["https://example.org/a.jpg", "https://example.org/b.jpg"]}
    assert primary.called
    assert [p["id"] for p in out["added"]] == [new] and out["added"][0]["primary"] is True
    assert out["failed"] == [{"url": "https://example.org/b.jpg", "error": "Download timeout"}]


@respx.mock
async def test_add_photos_when_every_url_fails_is_not_an_error():
    respx.post(f"{BASE}/api/images/import_from_urls/").mock(return_value=httpx.Response(400, json={
        "error": "No images could be imported",
        "results": [{"url": "https://example.org/a.jpg", "status": "failed", "error": "Invalid image URL"}]}))
    out = await _svc().add_photos("lodging", LOC, ["https://example.org/a.jpg"], None, False)
    assert out == {"item_type": "lodging", "item_id": LOC, "added": [],
                   "failed": [{"url": "https://example.org/a.jpg", "error": "Invalid image URL"}]}


@respx.mock
async def test_set_primary_photo_is_idempotent():
    respx.post(f"{BASE}/api/images/{IMG}/toggle_primary/").mock(
        return_value=httpx.Response(400, json={"error": "Image is already the primary image"}))
    assert (await _svc().set_primary_photo(IMG))["primary"] is True


def test_file_paths_refused_over_http(tmp_path):
    pic = tmp_path / "a.jpg"
    pic.write_bytes(b"\xff\xd8\xff")
    settings = Settings()
    assert _read_photos([str(pic)], settings) == [("a.jpg", b"\xff\xd8\xff")]
    settings.http = True
    with pytest.raises(ToolError, match="stdio"):
        _read_photos([str(pic)], settings)
    settings.http = False
    with pytest.raises(ToolError, match="not a photo"):
        _read_photos([str(tmp_path / "notes.txt")], settings)
