"""Time helpers owned by the stock research platform."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


CHINA_TIMEZONE = "Asia/Shanghai"


def get_china_now() -> datetime:
    """Return the current timezone-aware China Standard Time."""
    return datetime.now(ZoneInfo(CHINA_TIMEZONE))
