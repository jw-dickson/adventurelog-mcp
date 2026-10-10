"""Local-time <-> UTC helpers.

AdventureLog stores datetimes in UTC and keeps the IANA timezone alongside
(transportation start/end_timezone, lodging timezone, visit timezone). Tools
speak local wall-clock time, which is what a trip plan is written in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone {tz!r}; use an IANA name like 'Pacific/Auckland'") from exc


def to_utc_iso(local: str, tz: str | None) -> str:
    """'2026-12-15T10:30' in tz -> '2026-12-14T21:30:00Z'. Offset-aware input keeps its offset."""
    dt = datetime.fromisoformat(local.strip().replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(tz))
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def to_local(value: str | None, tz: str | None) -> str | None:
    """UTC ISO string -> 'YYYY-MM-DDTHH:MM' wall-clock time in tz."""
    dt = parse_utc(value)
    if dt is None:
        return None
    return dt.astimezone(zone(tz)).strftime("%Y-%m-%dT%H:%M")


def local_date(value: str | None, tz: str | None) -> str | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    return dt.astimezone(zone(tz)).date().isoformat()


def check_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()
