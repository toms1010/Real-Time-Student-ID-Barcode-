"""UTC time helpers.

Every timestamp written by this application is ISO-8601 in UTC with a trailing
``Z`` and millisecond precision, e.g. ``2026-09-30T09:42:17.412Z``.  Reports may
present the value in the configured display timezone, but storage stays UTC so a
report generated in one timezone is comparable with a report generated in
another.

SQLite's ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` default produces exactly this
format, which keeps SQL-side defaults and Python-side writes consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

#: 24 characters: "YYYY-MM-DDTHH:MM:SS.mmmZ".  Millisecond precision, matching
#: SQLite's `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` exactly, so a timestamp
#: written by Python and one written by a SQL default compare correctly as
#: strings *and* sort correctly in a `ORDER BY`.
SQL_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    return format_iso(utc_now())


def format_iso(value: datetime | None) -> str | None:
    """Normalise any datetime to the project's storage format.

    Milliseconds are truncated (never rounded up) so a timestamp can never be
    formatted as a time later than it really was.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return (
        f"{value:%Y-%m-%dT%H:%M:%S}."
        f"{value.microsecond // 1000:03d}Z"
    )


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse a stored timestamp, tolerating ``Z``, offsets and missing zone.

    Returns ``None`` for empty input.  Raises ``ValueError`` for anything that
    is not a recognisable timestamp - callers in request handlers convert that
    into a 422 rather than silently substituting "now".
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - exercised via API tests
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_local(value: datetime | str | None, tz_name: str = "UTC") -> datetime | None:
    """Convert a stored timestamp into the configured display timezone.

    Falls back to UTC when the zone is unknown (e.g. a slim container without
    tzdata) so a report never crashes on a cosmetic conversion.
    """
    parsed = parse_iso(value)
    if parsed is None:
        return None
    try:
        tz = _zone(tz_name)
    except Exception:  # noqa: BLE001 - cosmetic conversion must never fail a request
        return parsed
    return parsed.astimezone(tz)


def _zone(tz_name: str) -> timezone | object:
    if tz_name.upper() == "UTC":
        return UTC
    from zoneinfo import ZoneInfo  # local import: keeps startup cheap

    return ZoneInfo(tz_name)


def start_of_day(value: datetime | str | None = None) -> str:
    """ISO timestamp for 00:00:00.000Z of the given (or current) UTC day."""
    base = parse_iso(value) if value is not None else utc_now()
    assert base is not None
    day = base.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return format_iso(day)  # type: ignore[return-value]


def day_bounds(day: str) -> tuple[str, str]:
    """Return ``(start, end)`` ISO timestamps covering the given ``YYYY-MM-DD``."""
    start = parse_iso(f"{day}T00:00:00.000Z")
    assert start is not None
    end = start + timedelta(days=1)
    return format_iso(start), format_iso(end)  # type: ignore[return-value]


def elapsed_ms(start: datetime, end: datetime | None = None) -> float:
    """Milliseconds between two datetimes (non-negative, microsecond accuracy)."""
    finish = end or utc_now()
    return max(0.0, (finish - start).total_seconds() * 1000.0)
