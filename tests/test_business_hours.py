from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from recoveryroom_sauna.business_hours import (
    BusinessHoursPolicy, DailyHours, HoursError, bounded_timer_minutes, effective_deadline,
)

BNE = ZoneInfo("Australia/Brisbane")

WEEKLY = {
    "monday": {"open": "09:00", "close": "17:00"},
    "tuesday": {"open": "09:00", "close": "17:00"},
    "wednesday": {"open": "09:00", "close": "17:00"},
    "thursday": {"open": "09:00", "close": "19:00"},
    "friday": {"open": "09:00", "close": "17:00"},
    "saturday": {"open": "08:00", "close": "13:00"},
    "sunday": None,
}


def policy(**kw):
    data = {"weekly": WEEKLY, "dateOverrides": {}, **kw}
    return BusinessHoursPolicy.from_dict(data, revision="r1")


def bne(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BNE)


# ---------------------------------------------------------------- validation

def test_unconfigured_weekly_yields_no_windows():
    p = BusinessHoursPolicy.from_dict({"weekly": None})
    assert not p.configured
    assert p.window_for(date(2026, 11, 2)) is None
    assert p.next_window(bne(2026, 11, 2, 8)) is None
    assert p.current_window(bne(2026, 11, 2, 10)) is None


def test_incomplete_weekly_rejected():
    with pytest.raises(HoursError, match="missing sunday"):
        BusinessHoursPolicy.from_dict({"weekly": {k: v for k, v in WEEKLY.items() if k != "sunday"}})


@pytest.mark.parametrize("o,c", [("17:00", "09:00"), ("09:00", "09:00"), ("22:00", "02:00")])
def test_inverted_zero_and_overnight_rejected(o, c):
    with pytest.raises(HoursError):
        DailyHours.parse(o, c)


@pytest.mark.parametrize("bad", ["9:00", "0900", "24:00", "09:60", 900, None])
def test_bad_time_strings(bad):
    with pytest.raises(HoursError):
        DailyHours.parse(bad, "17:00")


def test_unknown_weekday_and_bad_override_keys():
    with pytest.raises(HoursError):
        BusinessHoursPolicy.from_dict({"weekly": {**WEEKLY, "funday": None}})
    with pytest.raises(HoursError):
        BusinessHoursPolicy.from_dict({"weekly": WEEKLY, "dateOverrides": {"25/12/2026": None}})
    with pytest.raises(HoursError):
        BusinessHoursPolicy.from_dict({"weekly": WEEKLY, "dateOverrides": {"2026-12-25": {"open": "09:00"}}})


def test_naive_datetime_rejected():
    with pytest.raises(HoursError):
        policy().current_window(datetime(2026, 11, 2, 10))


# ---------------------------------------------------------------- resolution

def test_every_weekday_resolves_with_30_30_margins():
    p = policy()
    monday = date(2026, 11, 2)
    expected = [("09:00", "17:00"), ("09:00", "17:00"), ("09:00", "17:00"), ("09:00", "19:00"),
                ("09:00", "17:00"), ("08:00", "13:00"), None]
    for i, exp in enumerate(expected):
        d = monday + timedelta(days=i)
        w = p.window_for(d)
        if exp is None:
            assert w is None
            continue
        assert w.opens_at == datetime.combine(d, time.fromisoformat(exp[0]), tzinfo=BNE)
        assert w.closes_at == datetime.combine(d, time.fromisoformat(exp[1]), tzinfo=BNE)
        assert w.preheat_eligible_at == w.opens_at - timedelta(minutes=30)
        assert w.latest_shutdown_at == w.closes_at + timedelta(minutes=30)
        assert w.revision == "r1" and w.source == "weekly"


def test_example_window_matches_handoff():
    w = policy().window_for(date(2026, 11, 2))
    assert w.preheat_eligible_at == bne(2026, 11, 2, 8, 30)
    assert w.latest_shutdown_at == bne(2026, 11, 2, 17, 30)


def test_override_closed_shortened_extended():
    p = policy(dateOverrides={"2026-12-25": None, "2026-12-24": {"open": "09:00", "close": "12:00"},
                              "2026-12-23": {"open": "07:00", "close": "21:00"}})
    assert p.window_for(date(2026, 12, 25)) is None            # Friday, normally open
    w = p.window_for(date(2026, 12, 24))
    assert w.closes_at == bne(2026, 12, 24, 12) and w.source == "override"
    w = p.window_for(date(2026, 12, 23))
    assert w.preheat_eligible_at == bne(2026, 12, 23, 6, 30) and w.latest_shutdown_at == bne(2026, 12, 23, 21, 30)


def test_override_can_open_a_normally_closed_day():
    p = policy(dateOverrides={"2026-11-08": {"open": "10:00", "close": "14:00"}})   # a Sunday
    assert p.window_for(date(2026, 11, 8)).opens_at == bne(2026, 11, 8, 10)


def test_current_window_boundaries():
    p = policy()
    assert p.current_window(bne(2026, 11, 2, 8, 29)) is None
    assert p.current_window(bne(2026, 11, 2, 8, 30)).business_date == date(2026, 11, 2)
    assert p.current_window(bne(2026, 11, 2, 17, 29)).business_date == date(2026, 11, 2)
    assert p.current_window(bne(2026, 11, 2, 17, 30)) is None
    assert p.current_window(bne(2026, 11, 8, 12)) is None      # Sunday closed


def test_grace_past_midnight_still_honoured():
    p = policy(dateOverrides={"2026-11-02": {"open": "18:00", "close": "23:45"}})
    assert p.current_window(bne(2026, 11, 3, 0, 10)).business_date == date(2026, 11, 2)
    assert p.current_window(bne(2026, 11, 3, 0, 15)) is None


def test_next_window_skips_closed_days_and_rolls_over_month_and_year():
    p = policy(dateOverrides={"2026-12-31": None, "2027-01-01": None})
    assert p.next_window(bne(2026, 11, 7, 9)).business_date == date(2026, 11, 9)       # Sat 09:00 is past 07:30 preheat -> Monday
    assert p.next_window(bne(2026, 11, 7, 7, 30)).business_date == date(2026, 11, 7)   # exactly at preheat counts
    assert p.next_window(bne(2026, 11, 30, 20)).business_date == date(2026, 12, 1)
    assert p.next_window(bne(2026, 12, 30, 20)).business_date == date(2027, 1, 2)      # Sat 2 Jan 2027


def test_next_window_none_when_everything_closed():
    p = BusinessHoursPolicy.from_dict({"weekly": {k: None for k in WEEKLY}})
    assert p.next_window(bne(2026, 11, 2, 8)) is None


def test_browser_timezone_does_not_matter():
    p = policy()
    # 2026-11-02 08:30 Brisbane == 2026-11-01 22:30 UTC == 2026-11-02 09:30 Sydney (AEDT)
    utc_now = datetime(2026, 11, 1, 22, 30, tzinfo=ZoneInfo("UTC"))
    syd_now = datetime(2026, 11, 2, 9, 30, tzinfo=ZoneInfo("Australia/Sydney"))
    for now in (utc_now, syd_now):
        assert p.local_date(now) == date(2026, 11, 2)
        assert p.current_window(now).preheat_eligible_at == now


def test_changed_revision_is_captured_on_window():
    p1 = BusinessHoursPolicy.from_dict({"weekly": WEEKLY}, revision="r1")
    p2 = BusinessHoursPolicy.from_dict({"weekly": {**WEEKLY, "monday": {"open": "10:00", "close": "16:00"}}}, revision="r2")
    d = date(2026, 11, 2)
    assert p1.window_for(d).revision == "r1" and p2.window_for(d).revision == "r2"
    assert p2.window_for(d).latest_shutdown_at < p1.window_for(d).latest_shutdown_at


def test_lead_and_grace_configurable_and_bounded():
    p = BusinessHoursPolicy.from_dict({"weekly": WEEKLY, "preheatLeadMinutes": 45, "shutdownAfterCloseMinutes": 0})
    w = p.window_for(date(2026, 11, 2))
    assert w.preheat_eligible_at == bne(2026, 11, 2, 8, 15) and w.latest_shutdown_at == w.closes_at
    with pytest.raises(HoursError):
        BusinessHoursPolicy.from_dict({"weekly": WEEKLY, "preheatLeadMinutes": 181})


# ---------------------------------------------------------------- deadline arithmetic

def test_effective_deadline_is_earlier_of_cutoff_and_continuous_limit():
    w = policy().window_for(date(2026, 11, 2))
    assert effective_deadline(w, None, None) == bne(2026, 11, 2, 17, 30)
    assert effective_deadline(w, bne(2026, 11, 2, 8, 30), 300) == bne(2026, 11, 2, 13, 30)
    assert effective_deadline(w, bne(2026, 11, 2, 14, 0), 300) == bne(2026, 11, 2, 17, 30)
    assert effective_deadline(w, bne(2026, 11, 2, 8, 30), None) == bne(2026, 11, 2, 17, 30)


@pytest.mark.parametrize("requested,remaining,expected", [
    (60, 10_000, 60),      # plenty left: hardware max
    (45, 10_000, 45),      # staff request wins
    (60, 3_600, 58),       # floor((3600-120)/60) = 58
    (60, 180, 1),          # exactly one safe minute
    (60, 179, None),       # fewer than one safe minute -> Stop
    (60, 120, None),
    (0, 10_000, None),
    (60, -5, None),
])
def test_bounded_timer_minutes(requested, remaining, expected):
    assert bounded_timer_minutes(requested, remaining) == expected


def test_bounded_timer_never_exceeds_hardware_max():
    assert bounded_timer_minutes(300, 10_000) == 60
    assert bounded_timer_minutes(300, 10_000, hardware_max_minutes=30) == 30
