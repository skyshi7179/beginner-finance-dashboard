"""Streamlit entry point for the beginner-oriented financial research platform."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.ai_report.page import render_ai_report_page
from src.backtest.page import render_backtest_page
from src.home_page import render_home_page
from src.navigation import (
    PAGE_AI_REPORT,
    PAGE_BACKTEST,
    PAGE_DATA_ACQUISITION,
    PAGE_DATA_MANAGEMENT,
    PAGE_HOME,
    PAGE_VISUALIZATION,
    get_current_page,
    navigate_to,
)
from src.stock_analysis.page import render_visualization_page
from src.stock_data.management import render_data_management
from src.stock_data.page import render_stock_data_page
from src.stock_data.session_cache import start_cleaner
from src.ui_experience import inject_platform_css, render_appearance, temporary_heartbeat


st.set_page_config(
    page_title="面向新手金融数据分析平台",
    page_icon=":material/query_stats:",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _open(page: str | None) -> None:
    """Apply a page result and rerun only when navigation was requested."""
    if page:
        navigate_to(st.session_state, page)
        st.rerun()


def main() -> None:
    """Render one explicit product page without loading retired modules."""
    inject_platform_css()
    render_appearance()
    start_cleaner(Path("data/session_cache"))
    temporary_heartbeat()
    page = get_current_page(st.session_state)

    if page == PAGE_HOME:
        _open(render_home_page())
    elif page == PAGE_DATA_ACQUISITION:
        if render_stock_data_page():
            _open(PAGE_HOME)
    elif page == PAGE_VISUALIZATION:
        _open(render_visualization_page())
    elif page == PAGE_BACKTEST:
        _open(render_backtest_page())
    elif page == PAGE_DATA_MANAGEMENT:
        _open(render_data_management())
    elif page == PAGE_AI_REPORT:
        _open(render_ai_report_page())
    else:
        _open(PAGE_HOME)


if __name__ == "__main__":
    main()
