"""Offline controls, explicit run, export and navigation checks."""

import json
from io import BytesIO
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.backtest.engine import BacktestParams, run_backtest
from src.backtest.charts import build_portfolio_chart, build_trade_chart
from src.backtest.export import export_run
from src.stock_data.cache import save_history_cache
from src.stock_data.provider import fetch_stock_history


APP = Path(__file__).resolve().parents[1] / "app.py"


def fixture_data(count=90):
    prices = 100 + 15 * np.sin(np.arange(count) / 4)
    return pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=count).strftime("%Y-%m-%d"),
                         "open": prices, "high": prices + 1, "low": prices - 1,
                         "close": prices, "volume": 10})


def seed_cache(directory, count=90):
    raw = fixture_data(count)
    frame = fetch_stock_history("600519", raw["date"].min(), raw["date"].max(), name="离线测试证券", market="上海", fetcher=lambda **kwargs: raw)
    save_history_cache(frame, {"source": "Tencent", "updated_at": "2024-06-01T12:00:00+08:00",
                               "coverage_ranges": [{"start": raw["date"].min(), "end": raw["date"].max()}]},
                        "600519", data_dir=directory)
    return frame


def open_page(directory, monkeypatch):
    import requests
    import src.backtest.page as page
    import src.stock_data.provider as provider

    calls = []
    original = page.run_backtest

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("backtest UI must not fetch a data provider")

    monkeypatch.setattr(page, "DATA_DIR", directory)
    monkeypatch.setattr(page, "run_backtest", run)
    monkeypatch.setattr(requests, "get", unexpected)
    monkeypatch.setattr(provider, "fetch_tencent_history", unexpected)
    monkeypatch.setattr(provider, "fetch_sina_turnover", unexpected)
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    return app, calls


def click_run(app):
    next(button for button in app.button if button.label == "运行回测").click().run()


def test_empty_cache_routes_to_acquisition_without_running(tmp_path, monkeypatch):
    app, calls = open_page(tmp_path, monkeypatch)
    assert calls == []
    assert not app.exception
    app.button(key="backtest_no_cache").click().run()
    assert app.session_state["app_page"] == "data_acquisition"


def test_explicit_run_charts_metrics_and_reruns_preserve_results(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch)
    assert calls == []
    assert any("研究单位" in item.value for item in app.warning)
    click_run(app)
    assert not app.exception
    assert len(calls) == 1
    assert len(app.get("plotly_chart")) == 3
    assert len(app.metric) == 4
    assert int(app.metric[3].value) > 0
    app.run()
    assert len(calls) == 1
    app.button(key="return_home_backtest").click().run()
    app.button(key="home_enter_backtest").click().run()
    assert not app.exception
    assert len(calls) == 1
    assert len(app.get("plotly_chart")) == 3
    assert app.number_input(key="backtest_short").value == 5
    app.button(key="backtest_visualization").click().run()
    assert app.session_state["app_page"] == "visualization"


def test_parameter_change_requires_new_run_and_bad_window_does_not_crash(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch)
    click_run(app)
    app.number_input(key="backtest_short").set_value(6)
    assert len(calls) == 1
    assert app.session_state["backtest_run_result"].params.short_window == 5
    click_run(app)
    assert len(calls) == 2
    assert app.session_state["backtest_run_result"].params.short_window == 6
    app.number_input(key="backtest_short").set_value(30)
    click_run(app)
    assert any("短期窗口" in item.value for item in app.error)
    assert not app.exception


def test_short_history_explains_instead_of_crashing(tmp_path, monkeypatch):
    seed_cache(tmp_path, count=6)
    app, calls = open_page(tmp_path, monkeypatch)
    click_run(app)
    assert any("数据长度不足" in item.value for item in app.error)
    assert not app.exception


def test_charts_have_run_parameters_and_signal_execution_are_distinct():
    result = run_backtest(fixture_data())
    for figure in [build_portfolio_chart(result, "600519"), build_portfolio_chart(result, "600519", drawdown=True), build_trade_chart(result, "600519")]:
        assert "MA5/20" in figure.layout.title.text
        assert "2024-01-02" in figure.layout.title.text
        assert "0.03%" in figure.layout.title.text
        assert "100,000" in figure.layout.title.text
        assert "无风险年利率" in figure.layout.title.text
    trade_chart = build_trade_chart(result, "600519")
    assert {trace.name for trace in trade_chart.data}.issuperset({"收盘金叉信号", "次日开盘模拟买入"})
    assert "研究价格" in trade_chart.layout.yaxis.title.text


def test_zip_has_reproducible_parameters_logs_and_daily_account():
    result = run_backtest(fixture_data())
    content = export_run(result, {"symbol": "600519", "source": "FIXTURE"})
    with zipfile.ZipFile(BytesIO(content)) as bundle:
        assert set(bundle.namelist()) == {"daily_equity.csv", "orders.csv", "filled_trades.csv", "benchmark_orders.csv", "performance.csv", "assumptions.json"}
        assumptions = json.loads(bundle.read("assumptions.json"))
        assert assumptions["params"]["short_window"] == 5
        assert assumptions["metadata"]["initial_shares"] == 0
        assert "not real CNY" in assumptions["units"]
        daily = pd.read_csv(bundle.open("daily_equity.csv"))
        assert daily["open"].iloc[0] == 100
        assert "input_open" in daily
        assert daily["strategy_cash"].ge(0).all()
        trades = pd.read_csv(bundle.open("filled_trades.csv"))
        assert (pd.to_datetime(trades["execution_date"]) > pd.to_datetime(trades["signal_date"])).all()
