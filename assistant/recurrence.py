"""Decide when recurring work runs next.

Two kinds live in ``task.metadata["schedule"]``:

``interval``
    Every ``every_seconds`` from the previous fire.
``daily``
    A wall-clock time of day (``at_hour``/``at_minute``) in a named timezone.

Everything here is a pure function of a spec and an instant, so the same code
decides the first run when a schedule is created, the next run after it fires,
and what the UI displays. Times cross the boundary as aware UTC datetimes; only
the daily kind is interpreted in the schedule's own timezone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

KIND_INTERVAL = "interval"
KIND_DAILY = "daily"

MIN_EVERY_SECONDS = 60
MAX_EVERY_SECONDS = 30 * 24 * 3600
MAX_TITLE_CHARS = 200

# ``UTC``, ``Z`` and ``GMT`` mean the same instant; everything else is either an
# explicit offset or a name that needs a timezone database.
_UTC_ALIASES = {"utc", "z", "gmt", "etc/utc"}
_OFFSET_RE = re.compile(r"^(?:UTC)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)

# A curated list for pickers. Users may still pass any IANA name.
COMMON_TIMEZONES = [
    "UTC",
    "Europe/Madrid",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Kolkata",
    "Australia/Sydney",
]


class ScheduleError(ValueError):
    """A schedule specification that cannot be honoured as written."""


@lru_cache(maxsize=1)
def timezone_database_available() -> bool:
    """True when IANA zone names can be resolved on this machine.

    Windows ships no system timezone database, so ``zoneinfo`` needs the
    ``tzdata`` package. Without it only ``UTC`` and fixed offsets can be
    honoured, and saying so beats silently interpreting a zone as UTC.
    """
    try:
        return bool(available_timezones())
    except Exception:  # pragma: no cover - depends on the host installation
        return False


def resolve_timezone(name: str | None, default: str = "UTC") -> tuple[str, tzinfo]:
    """Return the canonical name and tzinfo for a user-supplied timezone."""
    raw = (name or default or "UTC").strip()
    if not raw:
        raise ScheduleError("timezone must not be empty")
    if raw.lower() in _UTC_ALIASES:
        return "UTC", UTC
    offset = _OFFSET_RE.match(raw)
    if offset:
        sign = -1 if offset.group(1) == "-" else 1
        hours = int(offset.group(2))
        minutes = int(offset.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise ScheduleError(f"invalid UTC offset: {raw!r}")
        delta = sign * timedelta(hours=hours, minutes=minutes)
        label = f"UTC{'-' if sign < 0 else '+'}{hours:02d}:{minutes:02d}"
        return label, timezone(delta, label)
    try:
        return raw, ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as error:
        if timezone_database_available():
            raise ScheduleError(
                f"unknown timezone {raw!r}; use an IANA name such as Europe/Madrid"
            ) from error
        raise ScheduleError(
            f"cannot resolve timezone {raw!r}: no timezone database on this host. "
            "Install the 'tzdata' package, or use UTC / an offset like UTC+02:00"
        ) from error


def _bounded_seconds(value: Any) -> int:
    try:
        every = int(value)
    except (TypeError, ValueError) as error:
        raise ScheduleError("every_seconds must be an integer") from error
    if not MIN_EVERY_SECONDS <= every <= MAX_EVERY_SECONDS:
        raise ScheduleError(
            f"every_seconds must be between {MIN_EVERY_SECONDS} and {MAX_EVERY_SECONDS}"
        )
    return every


def _hour_minute(at_hour: Any, at_minute: Any) -> tuple[int, int]:
    try:
        hour = int(at_hour)
    except (TypeError, ValueError) as error:
        raise ScheduleError("at_hour must be an integer between 0 and 23") from error
    try:
        minute = int(at_minute if at_minute is not None else 0)
    except (TypeError, ValueError) as error:
        raise ScheduleError("at_minute must be an integer between 0 and 59") from error
    if not 0 <= hour <= 23:
        raise ScheduleError("at_hour must be between 0 and 23")
    if not 0 <= minute <= 59:
        raise ScheduleError("at_minute must be between 0 and 59")
    return hour, minute


def normalize_spec(
    *,
    every_seconds: Any = None,
    at_hour: Any = None,
    at_minute: Any = None,
    timezone_name: str | None = None,
    default_timezone: str = "UTC",
) -> dict[str, Any]:
    """Validate a schedule request and return its canonical specification.

    ``at_hour`` selects the daily kind; otherwise an interval is required.
    """
    if at_hour is not None:
        hour, minute = _hour_minute(at_hour, at_minute)
        canonical, _tz = resolve_timezone(timezone_name, default_timezone)
        return {
            "kind": KIND_DAILY,
            "every_seconds": None,
            "at_hour": hour,
            "at_minute": minute,
            "timezone": canonical,
        }
    if every_seconds is None:
        raise ScheduleError("provide every_seconds, or at_hour for a daily schedule")
    every = _bounded_seconds(every_seconds)
    # An interval is wall-clock independent: keep the label for display only.
    canonical, _tz = resolve_timezone(timezone_name, default_timezone)
    return {
        "kind": KIND_INTERVAL,
        "every_seconds": every,
        "at_hour": None,
        "at_minute": None,
        "timezone": canonical,
    }


def spec_from_record(schedule: dict[str, Any], default_timezone: str = "UTC") -> dict[str, Any]:
    """Read the specification back out of a stored schedule record.

    Records written before the daily kind existed carry no ``kind``; they are
    intervals, which is exactly what the default below restores.
    """
    if not isinstance(schedule, dict):
        raise ScheduleError("schedule must be an object")
    kind = schedule.get("kind")
    if kind == KIND_DAILY or (kind is None and schedule.get("at_hour") is not None):
        return normalize_spec(
            at_hour=schedule.get("at_hour"),
            at_minute=schedule.get("at_minute"),
            timezone_name=schedule.get("timezone"),
            default_timezone=default_timezone,
        )
    return normalize_spec(
        every_seconds=schedule.get("every_seconds"),
        timezone_name=schedule.get("timezone"),
        default_timezone=default_timezone,
    )


def _daily_candidate(spec: dict[str, Any], now: datetime) -> datetime:
    _name, tz = resolve_timezone(spec.get("timezone"), "UTC")
    local = now.astimezone(tz)
    candidate = local.replace(
        hour=int(spec["at_hour"]), minute=int(spec["at_minute"]), second=0, microsecond=0
    )
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_run(spec: dict[str, Any], now: datetime) -> datetime:
    """First instant strictly after ``now`` at which the schedule should run."""
    if spec.get("kind") == KIND_DAILY:
        return _daily_candidate(spec, now)
    return now + timedelta(seconds=int(spec["every_seconds"]))


def advance(spec: dict[str, Any], previous: datetime | None, now: datetime) -> datetime:
    """Next run at or after ``now``, given the previous one.

    Missed windows collapse into a single run: a laptop that was off for three
    days must not queue three days of work when it wakes up. Daily schedules
    never accumulate a backlog at all — a missed day is simply not made up.
    """
    if previous is not None and previous > now:
        return previous
    if spec.get("kind") == KIND_DAILY:
        return _daily_candidate(spec, now)
    every = int(spec["every_seconds"])
    moment = previous if previous is not None else now
    if moment > now:
        return moment
    steps = int((now - moment).total_seconds()) // every + 1
    return moment + timedelta(seconds=steps * every)


def describe(spec: dict[str, Any]) -> str:
    """Short human description, used by the API and the dashboard."""
    if spec.get("kind") == KIND_DAILY:
        zone = spec.get("timezone") or "UTC"
        suffix = "" if zone == "UTC" else f" ({zone})"
        return f"every day at {int(spec['at_hour']):02d}:{int(spec['at_minute']):02d}{suffix}"
    seconds = int(spec.get("every_seconds") or 0)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size and seconds % size == 0:
            return f"every {seconds // size} {unit}"
    return f"every {seconds} s"


def local_label(moment: datetime, timezone_name: str | None) -> str:
    """Render an instant in the schedule's own zone, for display only."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        _name, tz = resolve_timezone(timezone_name, "UTC")
    except ScheduleError:
        tz = UTC
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


# Re-exported for callers that used ``time`` fields directly.
__all__ = [
    "COMMON_TIMEZONES",
    "KIND_DAILY",
    "KIND_INTERVAL",
    "MAX_EVERY_SECONDS",
    "MIN_EVERY_SECONDS",
    "ScheduleError",
    "advance",
    "describe",
    "local_label",
    "next_run",
    "normalize_spec",
    "resolve_timezone",
    "spec_from_record",
    "timezone_database_available",
]
