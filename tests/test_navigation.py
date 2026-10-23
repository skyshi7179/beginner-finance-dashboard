from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.navigation import PAGE_HOME, get_current_page, navigate_to

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def test_navigation_helpers_default_and_reject_unknown_pages():
    state = {}
    assert get_current_page(state) == PAGE_HOME

    navigate_to(state, "not-a-real-page")
    assert state["app_page"] == PAGE_HOME


def test_home_shows_all_feature_and_advanced_entries():
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()

    assert "面向新手金融数据分析平台" in [item.value for item in app.title]
    for key in (
        "home_enter_data_acquisition",
        "home_enter_visualization",
        "home_enter_backtest",
        "home_enter_ai_report",
        "home_enter_data_management",
    ):
        assert app.button(key=key)


@pytest.mark.parametrize(
    ("page", "title"),
    (
        ("data_acquisition", "股票 / ETF 数据获取"),
        ("visualization", "金融数据可视化"),
        ("backtest", "自定义策略回测"),
    ),
)
def test_primary_feature_page_has_working_home_return(page, title):
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.button(key=f"home_enter_{page}").click().run()

    assert app.session_state["app_page"] == page
    assert title in [item.value for item in app.title]

    return_key = "stock_data_return_home" if page == "data_acquisition" else f"return_home_{page}"
    app.button(key=return_key).click().run()
    assert app.session_state["app_page"] == PAGE_HOME
    assert "面向新手金融数据分析平台" in [item.value for item in app.title]


def test_stock_data_page_reruns_do_not_fetch_without_button(monkeypatch):
    import src.stock_data.page as stock_page

    calls = []
    monkeypatch.setattr(stock_page, "get_stock_universe", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected universe request")))
    monkeypatch.setattr(stock_page, "get_stock_history", lambda *args, **kwargs: calls.append((args, kwargs)))
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.button(key="home_enter_data_acquisition").click().run()
    assert "股票 / ETF 数据获取" in [item.value for item in app.title]
    assert app.selectbox(key="stock_search_asset_type").options == ["A股", "ETF"]
    assert app.button(key="stock_refresh_universe").label == "刷新A股代码表"
    assert calls == []
    assert not app.exception


@pytest.mark.parametrize(
    ("page", "title", "status"),
    (
        ("data_management", "数据管理", "后续阶段"),
    ),
)
def test_data_management_has_working_home_return(page, title, status):
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.button(key=f"home_enter_{page}").click().run()

    assert app.session_state["app_page"] == page
    assert title in [item.value for item in app.title]
    assert app.button(key="cache_end_session")
    assert not app.exception

    app.button(key=f"return_home_{page}").click().run()
    assert app.session_state["app_page"] == PAGE_HOME

