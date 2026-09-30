"""Off-peak savings mode.

DeepSeek charges peak rates inside two daily windows (01:00-04:00 and
06:00-10:00 UTC, Monday to Friday, excluding Chinese public holidays) and half
of that the rest of the time. A persistent worker therefore has a real, direct
saving available: do not start paid work inside a peak window.

The policy is pure and deterministic so it can be unit-tested at any instant.
Holidays are configuration rather than a hardcoded guess: an unlisted holiday
only means the assistant pauses when it strictly did not need to, which is the
safe direction to be wrong in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

#: ``(start_hour, end_hour)`` in UTC, end exclusive. Monday to Friday only.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))
_WEEKEND_DAYS = {5, 6}
_MAX_PROBE_MINUTES = 8 * 24 * 60


def parse_holidays(raw: str | list[str] | None) -> tuple[set[date], list[str]]:
    """Parse ``YYYY-MM-DD`` entries, returning the valid dates and bad entries."""
    if not raw:
        return set(), []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    holidays: set[date] = set()
    invalid: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            holidays.add(date.fromisoformat(text))
        except ValueError:
            invalid.append(text)
    return holidays, invalid


class OffPeakPolicy:
    """Decide whether paid work should wait for an off-peak window."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        holidays: str | list[str] | None = None,
        windows: tuple[tuple[int, int], ...] = PEAK_WINDOWS_UTC,
    ):
        self.enabled = bool(enabled)
        self.windows = tuple(windows)
        self.holidays, self.invalid_holidays = parse_holidays(holidays)

    def in_peak(self, moment: datetime | None = None) -> bool:
        """True when ``moment`` falls inside a paid peak window."""
        current = (moment or datetime.now(UTC)).astimezone(UTC)
        if current.weekday() in _WEEKEND_DAYS:
            return False
        if current.date() in self.holidays:
            return False
        return any(start <= current.hour < end for start, end in self.windows)

    def is_paused(self, moment: datetime | None = None) -> bool:
        """True when savings mode is on and work must wait."""
        return self.enabled and self.in_peak(moment)

    def next_change(self, moment: datetime | None = None) -> datetime:
        """Instant when the current peak/off-peak state flips."""
        current = (moment or datetime.now(UTC)).astimezone(UTC)
        state = self.in_peak(current)
        probe = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_PROBE_MINUTES):
            if self.in_peak(probe) != state:
                return probe
            probe += timedelta(minutes=1)
        return probe

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def snapshot(self, moment: datetime | None = None) -> dict:
        """JSON-friendly state for the dashboard and the runtime heartbeat."""
        current = (moment or datetime.now(UTC)).astimezone(UTC)
        in_peak = self.in_peak(current)
        next_change = self.next_change(current)
        return {
            "enabled": self.enabled,
            "state": "PAUSED" if (self.enabled and in_peak) else "RUNNING",
            "in_peak": in_peak,
            "peak_windows_utc": [
                f"{start:02d}:00-{end:02d}:00" for start, end in self.windows
            ],
            "weekdays_only": True,
            "holidays_configured": len(self.holidays),
            "invalid_holidays": list(self.invalid_holidays),
            "next_change_at": next_change.isoformat(),
            "seconds_remaining": max(0, int((next_change - current).total_seconds())),
            "checked_at": current.isoformat(),
        }
