"""Shared explicit dates and completed daily-bar cutoff, without network calls."""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from src.stock_data.calendar import completed_end


def date_controls(prefix: str, default_start: date, default_end: date, *, all_start: date | None = None, acquisition: bool = False, all_end: date | None = None, selection_key: str = ""):
    """Keep the old tuple as durable state, but expose separate date widgets."""
    state = st.session_state
    all_label = "全部历史（上市以来，尽数据源所能）" if acquisition else "全部已缓存区间"
    if state.get(f"{prefix}_selection") != selection_key:
        state[f"{prefix}_selection"] = selection_key
        state[f"{prefix}_quick"] = "自定义"
    pending_until = state.pop(f"{prefix}_pending_until", None)
    if pending_until is not None:
        state[f"{prefix}_until"] = pending_until
    pending = state.pop(f"{prefix}_pending", None)
    if pending:
        state[f"{prefix}_start"], state[f"{prefix}_end"] = pending
    prior = state.get(prefix, (default_start, default_end))
    if not isinstance(prior, (tuple, list)) or len(prior) != 2:
        prior = (default_start, default_end)
    for suffix, value in (("start", prior[0]), ("end", prior[1])):
        if f"{prefix}_{suffix}" not in state:
            state[f"{prefix}_{suffix}"] = value
    cutoff, note = completed_end()
    preferences = state.get("date_control_preferences", {}).get(prefix, {})
    for suffix, fallback in (("until", False), ("quick", "自定义")):
        if f"{prefix}_{suffix}" not in state:
            state[f"{prefix}_{suffix}"] = preferences.get(suffix, fallback)
    if cutoff is None:
        state[f"{prefix}_until"] = False
    a, b, c = st.columns([3, 3, 2], vertical_alignment="bottom")
    until = c.checkbox("至今（完整日线）", key=f"{prefix}_until", disabled=cutoff is None)
    if until and cutoff:
        state[f"{prefix}_end"] = cutoff
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = a.date_input("开始日期", key=f"{prefix}_start", min_value=date(1990, 1, 1), max_value=today)
    end = b.date_input("结束日期", key=f"{prefix}_end", disabled=until, min_value=date(1990, 1, 1), max_value=today)
    # Manual dates may also not include an unfinished current daily candle.
    if cutoff and end > cutoff:
        st.warning(f"选择的结束日期尚无完整日线，本次计算/请求截至{cutoff}。")
        end = cutoff
    if until:
        st.caption(note + " 这里只调整日期；点击获取/更新才会联网。")
    applied = state.get(f"{prefix}_applied_all")
    if applied and (start,end,selection_key) != applied:
        state[f"{prefix}_quick"] = "自定义"
        state.pop(f"{prefix}_applied_all",None)
    quick = st.selectbox("快捷区间", ["自定义", "近1个月", "近3个月", "近1年", all_label], key=f"{prefix}_quick")
    if st.button("应用快捷区间", key=f"{prefix}_apply", disabled=quick == "自定义"):
        if quick == all_label:
            left = date(1990, 1, 1) if acquisition else all_start or default_start
            right = cutoff or end if acquisition else all_end or default_end
            state[prefix] = (left, right)
            state[f"{prefix}_applied_all"] = (left,right,selection_key)
            state[f"{prefix}_pending_until"] = bool(acquisition and cutoff)
        else:
            days = {"近1个月": 30, "近3个月": 90, "近1年": 365}[quick]
            state[prefix] = (end - timedelta(days=days), end)
        state[f"{prefix}_pending"] = state[prefix]
        st.rerun()
    if quick == all_label:
        st.caption("只设置日期，不自动联网。上市日期尚未核验时使用1990年起点查询，返回最早记录不等于已确认完整上市历史。" if acquisition else "只使用本地缓存实际范围，不自动获取更早行情。")
    state[prefix] = (start, end)
    state.date_control_preferences = {**state.get("date_control_preferences", {}), prefix:{"until":until,"quick":quick}}
    return start, end
