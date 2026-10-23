"""Offline provider-derived calendar and completed daily-bar cutoff.

Calendar checks are deliberately tri-state.  A date outside the bundled
coverage is *unknown* rather than silently treated as a closed day.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import json
from pathlib import Path
from zoneinfo import ZoneInfo

CALENDAR = Path(__file__).resolve().parents[2] / "config/trading_calendar.json"

SESSION_OPEN = "OPEN"
SESSION_CLOSED = "CLOSED"
SESSION_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SessionGap:
    """Calendar evidence between two observed daily bars."""

    status: str
    missing_sessions: tuple[str, ...] = ()

    @property
    def observation_is_next_session(self) -> bool:
        return self.status == "CONTIGUOUS"


@lru_cache(maxsize=4)
def _calendar(path, modified):
    return json.loads(Path(path).read_text())


def completed_end(now: datetime | None = None, calendar: dict | None = None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    try:
        calendar = calendar or _calendar(str(CALENDAR), CALENDAR.stat().st_mtime_ns)
        today = now.date().isoformat()
        if not calendar["coverage_start"] <= today <= calendar["coverage_end"]:
            return None, "本地交易日历未覆盖当前日期，无法确认最近完整交易日；请手动选择已有历史日期。"
        limit = now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)
        dates = [date.fromisoformat(d) for d in calendar["eligible_session_dates"] if d <= limit.isoformat()]
        if not dates:
            return None, "本地交易日历没有可用日期。"
        end = max(dates)
        note = ("今日已收盘，数据源可能延迟；最终以返回的最后行情日期为准。" if end == now.date()
                else f"仅使用已完成日线，当前截至{end}；不包含未收盘行情或非交易日。")
        return end, note + " 本地日期来自AKShare/Sina provider-derived交易日期，并非交易所权威日历。"
    except (OSError, ValueError, KeyError, TypeError):
        return None, "本地交易日历不可用，请手动选择已有历史日期。"



def market_session_status(day: str, calendar: dict | None = None) -> str:
    """Return OPEN, CLOSED or UNKNOWN for one calendar date."""
    try:
        calendar = calendar or _calendar(str(CALENDAR), CALENDAR.stat().st_mtime_ns)
        if not calendar["coverage_start"] <= day <= calendar["coverage_end"]:
            return SESSION_UNKNOWN
        return SESSION_OPEN if day in calendar["eligible_session_dates"] else SESSION_CLOSED
    except (OSError, ValueError, KeyError, TypeError):
        return SESSION_UNKNOWN


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def session_gap(previous, current, calendar: dict | None = None) -> SessionGap:
    """Classify the interval between two observed bars.

    Long exchange holidays remain CONTIGUOUS when no declared open session is
    skipped.  Unknown calendar coverage is reported separately and never
    described as a missing trading day.
    """
    try:
        before = _as_date(previous)
        after = _as_date(current)
        if after <= before:
            return SessionGap("INVALID")
        # Both observed endpoints must also lie on dates the bundled calendar
        # declares open.  An adjacent pair outside coverage has no intervening
        # day to inspect, but its continuity is still unknown.
        unknown = any(market_session_status(day.isoformat(), calendar) != SESSION_OPEN
                      for day in (before, after))
        missing = []
        day = before + timedelta(days=1)
        while day < after:
            status = market_session_status(day.isoformat(), calendar)
            unknown |= status == SESSION_UNKNOWN
            if status == SESSION_OPEN:
                missing.append(day.isoformat())
            day += timedelta(days=1)
        if missing:
            return SessionGap("MISSING_SESSION", tuple(missing))
        return SessionGap("UNKNOWN_CALENDAR" if unknown else "CONTIGUOUS")
    except (AttributeError, TypeError, ValueError):
        return SessionGap("INVALID")


def is_declared_trade_day(day: str) -> bool:
    """Backward-compatible boolean helper for callers that only need OPEN."""
    return market_session_status(day) == SESSION_OPEN
