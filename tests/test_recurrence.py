"""Recurrence math: intervals, wall-clock times and DST.

These are pure functions of a spec and an instant, so they are tested without a
database or a runtime. The Madrid cases are real transitions from the tzdata
calendar, which is the only way to prove a daily 08:00 survives a DST change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.recurrence import (
    MAX_EVERY_SECONDS,
    MIN_EVERY_SECONDS,
    ScheduleError,
    advance,
    describe,
    normalize_spec,
    resolve_timezone,
    spec_from_record,
    timezone_database_available,
)

needs_tzdata = pytest.mark.skipif(
    not timezone_database_available(), reason="no timezone database on this host"
)


def test_normalize_defaults_to_an_interval():
    spec = normalize_spec(every_seconds=900)

    assert spec["kind"] == "interval"
    assert spec["every_seconds"] == 900
    assert spec["at_hour"] is None
    assert spec["timezone"] == "UTC"


def test_normalize_requires_one_of_the_two_kinds():
    with pytest.raises(ScheduleError, match="every_seconds"):
        normalize_spec()


@pytest.mark.parametrize("every", [0, 5, MIN_EVERY_SECONDS - 1, MAX_EVERY_SECONDS + 1])
def test_intervals_outside_the_bounds_are_rejected(every):
    with pytest.raises(ScheduleError, match="between"):
        normalize_spec(every_seconds=every)


def test_the_bounds_themselves_are_accepted():
    assert normalize_spec(every_seconds=MIN_EVERY_SECONDS)["every_seconds"] == MIN_EVERY_SECONDS
    assert normalize_spec(every_seconds=MAX_EVERY_SECONDS)["every_seconds"] == MAX_EVERY_SECONDS


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_daily_hours_outside_the_clock_are_rejected(hour):
    with pytest.raises(ScheduleError, match="at_hour"):
        normalize_spec(at_hour=hour)


def test_daily_minutes_outside_the_clock_are_rejected():
    with pytest.raises(ScheduleError, match="at_minute"):
        normalize_spec(at_hour=8, at_minute=60)


def test_daily_defaults_to_the_top_of_the_hour():
    spec = normalize_spec(at_hour=8)

    assert spec["kind"] == "daily"
    assert (spec["at_hour"], spec["at_minute"]) == (8, 0)
    assert spec["every_seconds"] is None


def test_utc_aliases_resolve_without_a_timezone_database():
    for name in ("UTC", "utc", "Z", "GMT", "Etc/UTC"):
        canonical, zone = resolve_timezone(name)

        assert canonical == "UTC"
        assert zone.utcoffset(None) == timedelta(0)


@pytest.mark.parametrize(
    ("name", "label", "minutes"),
    [
        ("+02:00", "UTC+02:00", 120),
        ("UTC+02:00", "UTC+02:00", 120),
        ("-0530", "UTC-05:30", -330),
        ("utc+5", "UTC+05:00", 300),
    ],
)
def test_fixed_offsets_are_supported(name, label, minutes):
    canonical, zone = resolve_timezone(name)

    assert canonical == label
    assert zone.utcoffset(None) == timedelta(minutes=minutes)


def test_an_impossible_offset_is_rejected():
    with pytest.raises(ScheduleError, match="offset"):
        resolve_timezone("UTC+25:00")


def test_an_unknown_zone_is_reported_as_such():
    with pytest.raises(ScheduleError) as error:
        resolve_timezone("Not/AZone")

    assert "Not/AZone" in str(error.value)


@needs_tzdata
def test_an_iana_zone_is_resolved_by_its_own_rules():
    canonical, zone = resolve_timezone("Europe/Madrid")

    assert canonical == "Europe/Madrid"
    # Madrid is +01:00 in winter and +02:00 in summer.
    assert zone.utcoffset(datetime(2026, 1, 15, 12, tzinfo=UTC)) == timedelta(hours=1)
    assert zone.utcoffset(datetime(2026, 7, 15, 12, tzinfo=UTC)) == timedelta(hours=2)


def test_an_interval_starts_one_period_after_now():
    spec = normalize_spec(every_seconds=3600)
    now = datetime(2026, 9, 30, 10, 15, tzinfo=UTC)

    assert advance(spec, None, now) == now + timedelta(hours=1)


def test_missed_interval_windows_collapse_and_keep_the_phase():
    spec = normalize_spec(every_seconds=3600)
    now = datetime(2026, 9, 30, 10, 15, tzinfo=UTC)
    previous = now - timedelta(days=3, minutes=40)

    following = advance(spec, previous, now)

    assert following > now
    assert following - previous == timedelta(hours=73)
    assert (following - previous) % timedelta(hours=1) == timedelta(0)


def test_an_interval_whose_window_has_not_come_keeps_its_date():
    spec = normalize_spec(every_seconds=3600)
    now = datetime(2026, 9, 30, 10, 15, tzinfo=UTC)
    previous = now + timedelta(minutes=20)

    assert advance(spec, previous, now) == previous


@needs_tzdata
def test_a_daily_schedule_keeps_its_local_hour_across_a_dst_change():
    spec = normalize_spec(at_hour=8, timezone_name="Europe/Madrid")

    before = datetime(2026, 3, 28, 20, 0, tzinfo=UTC)
    after = datetime(2026, 3, 29, 20, 0, tzinfo=UTC)
    first = advance(spec, None, before)
    second = advance(spec, None, after)

    # 29 March 2026 is the spring change in Madrid: the clock jumps 02:00 ->
    # 03:00 local, so 08:00 local is 06:00 UTC from that day on (it was 07:00 UTC
    # the day before). The local hour is what stays fixed, not the UTC instant.
    assert first == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
    assert second == datetime(2026, 3, 30, 6, 0, tzinfo=UTC)

    winter_zone = resolve_timezone("Europe/Madrid")[1]
    assert first.astimezone(winter_zone).hour == 8
    assert second.astimezone(winter_zone).hour == 8
    # The same 08:00 local on the previous day is a different UTC instant.
    previous_day = advance(spec, None, datetime(2026, 3, 27, 20, 0, tzinfo=UTC))
    assert previous_day == datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    assert previous_day.astimezone(winter_zone).hour == 8


@needs_tzdata
def test_a_daily_schedule_in_summer_uses_the_summer_offset():
    # 20 October 2026 is still CEST in Madrid; the change to CET is on the 25th.
    spec = normalize_spec(at_hour=8, at_minute=30, timezone_name="Europe/Madrid")

    following = advance(spec, None, datetime(2026, 10, 20, 6, 0, tzinfo=UTC))

    assert following == datetime(2026, 10, 20, 6, 30, tzinfo=UTC)


def test_a_daily_schedule_rolls_over_to_tomorrow_when_the_hour_has_passed():
    spec = normalize_spec(at_hour=8)
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)

    assert advance(spec, None, now) == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_a_daily_schedule_missed_for_days_does_not_make_them_up():
    spec = normalize_spec(at_hour=8)
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    previous = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)

    following = advance(spec, previous, now)

    assert following == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_legacy_records_without_a_kind_are_intervals():
    spec = spec_from_record({"every_seconds": 600, "enabled": True, "runs": 3})

    assert spec["kind"] == "interval"
    assert spec["every_seconds"] == 600


def test_a_record_with_at_hour_is_daily_even_without_a_kind():
    spec = spec_from_record({"at_hour": 7, "at_minute": 15, "enabled": True})

    assert spec["kind"] == "daily"
    assert (spec["at_hour"], spec["at_minute"]) == (7, 15)


def test_an_unreadable_record_raises_instead_of_guessing():
    with pytest.raises(ScheduleError):
        spec_from_record({"enabled": True})

    with pytest.raises(ScheduleError):
        spec_from_record("every 5 minutes")


def test_a_default_timezone_is_applied_when_none_is_given():
    spec = normalize_spec(at_hour=8, default_timezone="UTC+02:00")

    assert spec["timezone"] == "UTC+02:00"


def test_describe_reads_naturally_for_both_kinds():
    assert describe(normalize_spec(every_seconds=1800)) == "every 30 min"
    assert describe(normalize_spec(every_seconds=86400)) == "every 1 d"
    assert describe(normalize_spec(every_seconds=90)) == "every 90 s"
    assert describe(normalize_spec(at_hour=8, at_minute=5)) == "every day at 08:05"
    assert (
        describe(normalize_spec(at_hour=8, timezone_name="UTC+02:00"))
        == "every day at 08:00 (UTC+02:00)"
    )
