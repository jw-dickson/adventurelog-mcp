"""Photo tools against a live AdventureLog (scratch trip + location, removed afterwards)."""

from __future__ import annotations

import os
import struct
import uuid
import zlib

import pytest
from mcp import Client

from adventurelog_mcp.server import build_server

from .test_live import Session

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ADVENTURELOG_URL") and os.environ.get("ADVENTURELOG_API_KEY")),
    reason="needs a live AdventureLog (ADVENTURELOG_URL + ADVENTURELOG_API_KEY)",
)


def _png(rgb: tuple[int, int, int], size: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


async def test_photos(tmp_path):
    red, blue = tmp_path / "red.png", tmp_path / "blue.png"
    red.write_bytes(_png((200, 30, 30)))
    blue.write_bytes(_png((30, 30, 200)))
    async with Client(build_server()) as client:
        call = Session(client)
        name = f"mcp-it-photos-{uuid.uuid4().hex[:8]}"
        trip = await call("create_trip", name=name, start_date="2027-04-01", end_date="2027-04-02",
                          description="adventurelog-mcp photo test; safe to delete")
        tid = trip["id"]
        loc = None
        try:
            loc = await call("upsert_location", trip_id=tid, name="Photo Spot", latitude=38.7, longitude=-9.1,
                             date="2027-04-01")

            added = await call("add_photos", item_type="location", item_id=loc["id"],
                               file_paths=[str(red), str(blue)])
            assert len(added["added"]) == 2 and added["failed"] == []
            first, second = (p["id"] for p in added["added"])

            # AdventureLog refuses to fetch private addresses; that is reported per URL, not raised
            refused = await call("add_photos", item_type="location", item_id=loc["id"],
                                 urls=["http://127.0.0.1/x.jpg"])
            assert refused["added"] == [] and refused["failed"][0]["url"] == "http://127.0.0.1/x.jpg"

            assert "Input should be" in await call.error(
                "add_photos", item_type="note", item_id=loc["id"], urls=["https://example.org/a.jpg"])

            await call("set_primary_photo", photo_id=second)
            await call("set_primary_photo", photo_id=second)  # idempotent
            item = await call("get_item", item_type="location", item_id=loc["id"])
            assert {p["id"]: p["primary"] for p in item["photos"]} == {first: False, second: True}
            assert all(p["url"] for p in item["photos"])

            cover = await call("set_trip_cover", trip_id=tid, photo_id=second)
            assert cover["cover_photo"]["id"] == second
            t = await call("get_trip", trip_id=tid)
            assert t["cover_photo"]["id"] == second and t["days"][0]["items"][0]["photos"] == 2

            await call("remove_photo", photo_id=first)
            item = await call("get_item", item_type="location", item_id=loc["id"])
            assert [p["id"] for p in item["photos"]] == [second]

            cleared = await call("set_trip_cover", trip_id=tid)
            assert cleared.get("cover_photo") is None
        finally:
            if loc:
                await call("delete_item", item_type="location", item_id=loc["id"])
            await call("delete_trip", trip_id=tid, confirm_name=name)
