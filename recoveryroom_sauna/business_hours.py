"""Operating-hours policy: schedule resolution and the fixed daily deadline.

NEW commercial logic (not from the home reference). Pure functions over an
explicit "now" so the whole policy can be tested with a fake clock across
many business days. Nothing here talks to the sauna or the clock.

Policy (handoff section 3):
  * timezone Australia/Brisbane; instants are tz-aware, business dates local
  * one open/close interval per weekday or CLOSED; incomplete, inverted,
    zero-length and overnight intervals are rejected
  * date overrides: closed, shortened or extended hours for a specific date
  * preheat eligibility = open - preheatLeadMinutes (default 30)
  * latest shutdown   = close + shutdownAfterCloseMinutes (default 30)
  * the effective deadline is the earlier of that cutoff and any approved
    continuous-operation limit measured from the actual start
  * weekly hours unset (None) means scheduling is disabled, never "open all day"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Australia/Brisbane"
DEFAULT_PREHEAT_LEAD_MINUTES = 30
DEFAULT_SHUTDOWN_AFTER_CLOSE_MINUTES = 30
DELIVERY_RESERVE_SECONDS = 120
HARDWARE_TIMER_MAX_MINUTES = 60
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class HoursError(ValueError):
    """Invalid hours configuration."""


@dataclass(frozen=True)
class DailyHours:
    open: time
    close: time

    def __post_init__(self) -> None:
        if self.open.tzinfo is not None or self.close.tzinfo is not None:
            raise HoursError("open/close must be naive local times")
        if self.open.second or self.open.microsecond or self.close.second or self.close.microsecond:
            raise HoursError("open/close must be whole minutes")
        if self.close <= self.open:
            raise HoursError(
                f"interval {self.open:%H:%M}-{self.close:%H:%M} is inverted, zero-length or overnight; "
                "overnight hours are rejected in version one"
            )

    @classmethod
    def parse(cls, open_s: str, close_s: str) -> "DailyHours":
        return cls(_parse_hhmm(open_s), _parse_hhmm(close_s))

    def to_dict(self) -> dict:
        return {"open": self.open.strftime("%H:%M"), "close": self.close.strftime("%H:%M")}


def _parse_hhmm(s: str) -> time:
    if not isinstance(s, str) or len(s) != 5 or s[2] != ":":
        raise HoursError(f"time {s!r} must be HH:MM")
    try:
        return time(int(s[:2]), int(s[3:]))
    except ValueError as exc:
        raise HoursError(f"time {s!r} invalid: {exc}") from exc


@dataclass(frozen=True)
class OperatingWindow:
    """One business date's approved window, resolved to tz-aware instants."""

    business_date: date
    opens_at: datetime
    closes_at: datetime
    preheat_eligible_at: datetime
    latest_shutdown_at: datetime
    revision: str
    source: str                     # "weekly" or "override"

    def contains(self, now: datetime) -> bool:
        return self.preheat_eligible_at <= now < self.latest_shutdown_at

    def to_dict(self) -> dict:
        return {
            "businessDate": self.business_date.isoformat(),
            "opensAt": self.opens_at.isoformat(),
            "closesAt": self.closes_at.isoformat(),
            "preheatEligibleAt": self.preheat_eligible_at.isoformat(),
            "latestShutdownAt": self.latest_shutdown_at.isoformat(),
            "revision": self.revision,
            "source": self.source,
        }


@dataclass(frozen=True)
class BusinessHoursPolicy:
    """Validated, immutable policy revision."""

    weekly: dict[int, DailyHours | None] | None       # 0=Monday .. 6=Sunday; None => unconfigured
    overrides: dict[date, DailyHours | None] = field(default_factory=dict)   # None => closed that date
    preheat_lead_minutes: int = DEFAULT_PREHEAT_LEAD_MINUTES
    shutdown_after_close_minutes: int = DEFAULT_SHUTDOWN_AFTER_CLOSE_MINUTES
    timezone: str = DEFAULT_TIMEZONE
    revision: str = "unset"

    def __post_init__(self) -> None:
        if not (0 <= self.preheat_lead_minutes <= 180):
            raise HoursError("preheatLeadMinutes must be 0-180")
        if not (0 <= self.shutdown_after_close_minutes <= 180):
            raise HoursError("shutdownAfterCloseMinutes must be 0-180")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise HoursError(f"unknown timezone {self.timezone!r}") from exc
        if self.weekly is not None:
            missing = [WEEKDAY_NAMES[d] for d in range(7) if d not in self.weekly]
            if missing:
                raise HoursError(f"weekly hours incomplete; missing {', '.join(missing)} (use null for CLOSED)")
            extra = [k for k in self.weekly if k not in range(7)]
            if extra:
                raise HoursError(f"weekly hours has invalid weekday keys {extra}")

    # ------------------------------------------------------------ construction

    @classmethod
    def from_dict(cls, data: dict | None, revision: str = "unset") -> "BusinessHoursPolicy":
        data = data or {}
        weekly_raw = data.get("weekly")
        weekly: dict[int, DailyHours | None] | None
        if weekly_raw is None:
            weekly = None
        else:
            if not isinstance(weekly_raw, dict):
                raise HoursError("weekly must be an object keyed by weekday name")
            weekly = {}
            for key, value in weekly_raw.items():
                if key not in WEEKDAY_NAMES:
                    raise HoursError(f"unknown weekday {key!r}")
                weekly[WEEKDAY_NAMES.index(key)] = _parse_hours_value(value, f"weekly.{key}")
        overrides_raw = data.get("dateOverrides") or {}
        if not isinstance(overrides_raw, dict):
            raise HoursError("dateOverrides must be an object keyed by YYYY-MM-DD")
        overrides: dict[date, DailyHours | None] = {}
        for key, value in overrides_raw.items():
            try:
                d = date.fromisoformat(key)
            except ValueError as exc:
                raise HoursError(f"override date {key!r} is not YYYY-MM-DD") from exc
            overrides[d] = _parse_hours_value(value, f"dateOverrides.{key}")
        return cls(
            weekly=weekly,
            overrides=overrides,
            preheat_lead_minutes=int(data.get("preheatLeadMinutes", DEFAULT_PREHEAT_LEAD_MINUTES)),
            shutdown_after_close_minutes=int(data.get("shutdownAfterCloseMinutes", DEFAULT_SHUTDOWN_AFTER_CLOSE_MINUTES)),
            timezone=str(data.get("timezone", DEFAULT_TIMEZONE)),
            revision=revision,
        )

    # --------------------------------------------------------------- queries

    @property
    def configured(self) -> bool:
        return self.weekly is not None

    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.timezone)

    def hours_for(self, business_date: date) -> tuple[DailyHours | None, str]:
        if business_date in self.overrides:
            return self.overrides[business_date], "override"
        if self.weekly is None:
            return None, "unconfigured"
        return self.weekly[business_date.weekday()], "weekly"

    def window_for(self, business_date: date) -> OperatingWindow | None:
        hours, source = self.hours_for(business_date)
        if hours is None:
            return None
        tz = self.tz
        opens = datetime.combine(business_date, hours.open, tzinfo=tz)
        closes = datetime.combine(business_date, hours.close, tzinfo=tz)
        return OperatingWindow(
            business_date=business_date,
            opens_at=opens,
            closes_at=closes,
            preheat_eligible_at=opens - timedelta(minutes=self.preheat_lead_minutes),
            latest_shutdown_at=closes + timedelta(minutes=self.shutdown_after_close_minutes),
            revision=self.revision,
            source=source,
        )

    def local_date(self, now: datetime) -> date:
        _require_aware(now)
        return now.astimezone(self.tz).date()

    def current_window(self, now: datetime) -> OperatingWindow | None:
        """Window whose [preheat, latest shutdown) contains now, if any.

        Checks yesterday too so a window that spilled past midnight via the
        closing grace is still honoured (overnight hours themselves are rejected).
        """
        today = self.local_date(now)
        for d in (today - timedelta(days=1), today):
            w = self.window_for(d)
            if w is not None and w.contains(now):
                return w
        return None

    def next_window(self, now: datetime, lookahead_days: int = 60) -> OperatingWindow | None:
        """Earliest window whose preheat time is at or after now."""
        today = self.local_date(now)
        for i in range(lookahead_days + 1):
            w = self.window_for(today + timedelta(days=i))
            if w is not None and w.preheat_eligible_at >= now:
                return w
        return None

    def to_dict(self) -> dict:
        weekly = None
        if self.weekly is not None:
            weekly = {WEEKDAY_NAMES[d]: (h.to_dict() if h else None) for d, h in sorted(self.weekly.items())}
        return {
            "configured": self.configured,
            "revision": self.revision,
            "timezone": self.timezone,
            "weekly": weekly,
            "dateOverrides": {d.isoformat(): (h.to_dict() if h else None) for d, h in sorted(self.overrides.items())},
            "preheatLeadMinutes": self.preheat_lead_minutes,
            "shutdownAfterCloseMinutes": self.shutdown_after_close_minutes,
        }


def _parse_hours_value(value, label: str) -> DailyHours | None:
    if value is None or value == "closed" or value == "CLOSED":
        return None
    if not isinstance(value, dict) or set(value) != {"open", "close"}:
        raise HoursError(f"{label} must be null/'closed' or an object with exactly open and close")
    return DailyHours.parse(value["open"], value["close"])


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise HoursError("instants must be timezone-aware")


# --------------------------------------------------------------------------- #
# Deadline arithmetic shared by every heating path (handoff section 4)
# --------------------------------------------------------------------------- #

def effective_deadline(
    window: OperatingWindow,
    started_at: datetime | None,
    continuous_limit_minutes: int | None,
) -> datetime:
    """Earlier of (close + grace) and (start + approved continuous limit).

    If no continuous limit is approved yet, the caller must block heating
    entirely (config.scheduled_heating_blockers); this function still returns
    the closing bound so the UI can display it.
    """
    deadline = window.latest_shutdown_at
    if started_at is not None and continuous_limit_minutes is not None:
        _require_aware(started_at)
        deadline = min(deadline, started_at + timedelta(minutes=continuous_limit_minutes))
    return deadline


def bounded_timer_minutes(
    requested_minutes: int,
    remaining_seconds: float,
    *,
    reserve_seconds: int = DELIVERY_RESERVE_SECONDS,
    hardware_max_minutes: int = HARDWARE_TIMER_MAX_MINUTES,
) -> int | None:
    """min(hardware max, staff request, floor((remaining - reserve)/60)).

    Returns None when fewer than one safe minute remains: the caller must
    Stop instead of issuing a timer.
    """
    if requested_minutes < 1:
        return None
    safe = int((remaining_seconds - reserve_seconds) // 60)
    minutes = min(hardware_max_minutes, requested_minutes, safe)
    return minutes if minutes >= 1 else None
