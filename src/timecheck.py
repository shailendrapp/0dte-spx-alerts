"""ET-aware time gating.  GitHub Actions cron runs in UTC and has jitter,
so each script self-checks that we're within a window around its scheduled
time before doing real work."""
from __future__ import annotations
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


def now_et() -> datetime:
    return datetime.now(tz=ET)


def today_et() -> date:
    return now_et().date()


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def within_window(target_hhmm: str, tolerance_min: int) -> bool:
    """True if current ET time is within ±tolerance_min of target."""
    now = now_et()
    target_t = parse_hhmm(target_hhmm)
    target_dt = datetime.combine(now.date(), target_t, tzinfo=ET)
    delta = abs((now - target_dt).total_seconds()) / 60.0
    log.info("Time check: now=%s ET, target=%s, delta=%.1f min, tolerance=%d",
             now.strftime("%H:%M:%S"), target_hhmm, delta, tolerance_min)
    return delta <= tolerance_min


def is_us_market_weekday(d: date | None = None) -> bool:
    """True if Monday-Friday.  (Holidays are caught separately via Tradier clock.)"""
    return (d or today_et()).weekday() < 5
