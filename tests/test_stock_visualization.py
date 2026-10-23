"""Offline mathematical, chart, cache and Streamlit workflow verification."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.stock_analysis.indicators import AnalysisInputError, calculate_indicators
from src.stock_analysis.charts import build_price_chart, build_return_chart
from src.stock_data.cache import save_history_cache
from src.stock_data.provider import fetch_stock_history
from src.stock_data.service import list_cached_stock_history


APP = Path(__file__).resolve().parents[1] / "app.py"


def history(closes=(100, 110, 99, 120, 90)):
    values = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=len(values)),
                         "open": values, "high": values + 1, "low": values - 1,
                         "close": values, "volume": np.full(len(values), 1000),
                         "adjustment": "qfq", "source": "Tencent", "symbol": "600519"})


def seed_cache(path, closes=tuple(range(100, 130)), *, asset_type="A股", adjustment="qfq", symbol="600519"):
    raw = history(closes).assign(symbol=symbol).drop(columns=["adjustment", "source"])
    raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
    raw["volume"] = 10  # Tencent lots, becomes 1000 shares/units.
    market = "深圳" if symbol.startswith(("0", "1", "3")) else "上海"
    frame = fetch_stock_history(symbol, raw["date"].min(), raw["date"].max(), name="离线测试证券", market=market,
                                 asset_type=asset_type, adjustment=adjustment, fetcher=lambda **kwargs: raw)
    metadata = {"symbol": symbol, "source": "Tencent", "adjustment": adjustment, "updated_at": "2024-03-01T12:00:00+08:00",
                "coverage_ranges": [{"start": raw["date"].min(), "end": raw["date"].max()}]}
    save_history_cache(frame, metadata, symbol, adjustment=adjustment, data_dir=path, asset_type=asset_type)
    return frame


def test_simple_returns_normalization_and_max_drawdown():
    result = calculate_indicators(history())
    data = result.data
    assert pd.isna(data.loc[0, "daily_return"])
    np.testing.assert_allclose(data["daily_return"].iloc[1:], [.1, -.1, 120 / 99 - 1, -.25])
    np.testing.assert_allclose(data["net_value"], [1, 1.1, .99, 1.2, .9])
    np.testing.assert_allclose(data["cumulative_return"], [0, .1, -.01, .2, -.1], atol=1e-14)
    np.testing.assert_allclose(data["drawdown"], [0, 0, -.1, 0, -.25], atol=1e-14)
    assert result.metrics["max_drawdown"] == pytest.approx(.25)
    assert result.metrics["annualized_volatility"] == pytest.approx(data["daily_return"].dropna().std(ddof=1) * np.sqrt(252))
    assert result.metrics["mean_daily_return"] == pytest.approx(data["daily_return"].dropna().mean())
    assert result.metrics["valid_trading_days"] == 5


def test_ma_windows_and_no_future_values():
    prefix = calculate_indicators(history(range(10, 35))).data
    extended = calculate_indicators(history([*range(10, 35), 10000])).data
    assert prefix["ma5"].iloc[:4].isna().all()
    assert prefix.loc[4, "ma5"] == 12
    assert prefix["ma20"].iloc[:19].isna().all()
    assert prefix.loc[19, "ma20"] == 19.5
    pd.testing.assert_frame_equal(prefix, extended.iloc[:25].reset_index(drop=True))


def test_selected_sample_has_its_own_base_and_no_pre_window_warmup():
    raw = history(range(100, 130)).iloc[10:].copy()
    result = calculate_indicators(raw)
    assert result.data.loc[0, "net_value"] == 1
    assert result.data.loc[0, "cumulative_return"] == 0
    assert result.data["ma20"].iloc[:19].isna().all()
    assert result.metrics["cumulative_return"] == pytest.approx(129 / 110 - 1)


def test_annualization_parameter_and_input_immutability():
    raw = history()
    before = raw.copy(deep=True)
    result = calculate_indicators(raw, trading_days=244)
    expected = raw["close"].pct_change(fill_method=None).dropna().std(ddof=1) * np.sqrt(244)
    assert result.metrics["annualized_volatility"] == pytest.approx(expected)
    pd.testing.assert_frame_equal(raw, before)
    with pytest.raises(AnalysisInputError):
        calculate_indicators(raw, trading_days=0)


def test_all_zero_volume_is_empty_and_unknown_volume_does_not_infer_suspension():
    assert calculate_indicators(history().assign(volume=0)).data.empty
    unknown = calculate_indicators(history().assign(volume="missing"))
    assert len(unknown.data) == 5
    assert unknown.data["volume"].isna().all()


@pytest.mark.parametrize("closes", [(), (100,), (100, 100), (100, 110, 120), (100, 90, 80)])
def test_empty_short_flat_and_monotonic_series(closes):
    result = calculate_indicators(history(closes))
    assert result.metrics["valid_trading_days"] == len(closes)
    if len(closes) < 3:
        assert result.metrics["annualized_volatility"] is None
    if len(closes) == 0:
        assert result.metrics["max_drawdown"] is None
    elif list(closes) == sorted(closes):
        assert result.metrics["max_drawdown"] == 0
    for field in ("daily_return", "cumulative_return", "drawdown"):
        assert build_return_chart(result.data, field).to_dict()
    assert build_price_chart(result.data).to_dict()


@pytest.mark.parametrize("mutate", [
    lambda df: df.assign(close="bad"),
    lambda df: df.assign(close=np.nan),
    lambda df: df.assign(close=0),
    lambda df: df.assign(open=-1),
    lambda df: df.assign(high=1),
    lambda df: df.assign(date="bad"),
    lambda df: df.assign(adjustment=["qfq", "qfq", "none", "qfq", "qfq"]),
    lambda df: df.assign(source=["Tencent", "Tencent", "Sina", "Tencent", "Tencent"]),
    lambda df: pd.concat([df, df.iloc[[0]].assign(close=100.5)]),
])
def test_invalid_prices_dates_and_mixed_contracts_block_calculation(mutate):
    with pytest.raises(AnalysisInputError):
        calculate_indicators(mutate(history()))


def test_sorting_duplicate_dates_suspension_and_invalid_volume():
    raw = history().iloc[::-1].copy()
    raw.loc[1, "volume"] = 0
    raw.loc[2, "volume"] = np.nan
    result = calculate_indicators(pd.concat([raw, raw.iloc[[0]]]))
    assert result.data["date"].is_monotonic_increasing
    assert len(result.data) == 4
    assert pd.isna(result.data.loc[1, "daily_return"])
    assert result.data.loc[1, "observation_return"] == pytest.approx(-.01)
    assert any("零成交" in warning for warning in result.warnings)
    assert any("成交量无效" in warning for warning in result.warnings)
    assert any("重复" in warning for warning in result.warnings)


def test_kline_volume_dates_ma_switches_and_shared_axis():
    data = calculate_indicators(history(range(100, 125))).data
    figure = build_price_chart(data, moving_averages=(5,), volume_unit="份")
    assert [trace.name for trace in figure.data] == ["K线", "MA5", "成交量（份）"]
    assert list(figure.data[0].x) == list(figure.data[2].x)
    assert figure.layout.xaxis.matches == "x2"
    assert figure.layout.xaxis2.rangeslider.visible
    assert figure.layout.xaxis2.title.text == "时间范围缩略图：拖动白色把手选择查看区间"
    assert figure.layout.yaxis2.title.text == "成交量（份）"
    assert [trace.type for trace in build_price_chart(data, show_price=False).data] == ["bar"]
    assert [trace.type for trace in build_price_chart(data, show_volume=False, moving_averages=()).data] == ["candlestick"]


def block_network(monkeypatch):
    import requests
    import src.stock_data.provider as provider

    def unexpected(*args, **kwargs):
        pytest.fail("visualization must never request a data provider")

    monkeypatch.setattr(requests, "get", unexpected)
    monkeypatch.setattr(provider, "fetch_tencent_history", unexpected)
    monkeypatch.setattr(provider, "fetch_sina_turnover", unexpected)


def open_visualization(tmp_path, monkeypatch):
    import src.stock_analysis.page as page
    monkeypatch.setattr(page, "DATA_DIR", tmp_path)
    block_network(monkeypatch)
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_visualization").click().run()
    return app


def test_catalog_reads_only_local_cache_and_keeps_modes_separate(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    seed_cache(tmp_path, adjustment="hfq")
    seed_cache(tmp_path, symbol="159516", asset_type="ETF")
    block_network(monkeypatch)
    entries, errors = list_cached_stock_history(tmp_path)
    assert not errors
    assert {(item["symbol"], item["asset_type"], item["adjustment"]) for item in entries} == {
        ("600519", "A股", "qfq"), ("600519", "A股", "hfq"), ("159516", "ETF", "qfq")}


def test_empty_page_guides_to_acquisition_without_network(tmp_path, monkeypatch):
    app = open_visualization(tmp_path, monkeypatch)
    assert not app.exception
    assert any("尚无本地行情缓存" in message.value for message in app.info)
    app.button(key="visualization_no_cache").click().run()
    assert app.session_state["app_page"] == "data_acquisition"


def test_page_metrics_charts_controls_and_home_roundtrip(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    app = open_visualization(tmp_path, monkeypatch)
    assert not app.exception
    assert len(app.get("plotly_chart")) == 4
    assert app.metric[0].value == "29.00%"
    assert app.metric[4].value == "30"
    assert next(v.value for v in app.dataframe if "period_cumulative_return" in v.value)["period_cumulative_return"].iloc[0] == pytest.approx(.29)
    assert next(v.value for v in app.dataframe if "period_valid_trading_days" in v.value)["period_valid_trading_days"].iloc[0] == 30
    app.multiselect(key="visualization_ma").set_value([5]).run()
    app.multiselect(key="visualization_charts").set_value(["K线", "成交量"]).run()
    assert len(app.get("plotly_chart")) == 1
    app.date_input(key="visualization_dates_start").set_value(date(2024, 1, 9))
    app.date_input(key="visualization_dates_end").set_value(date(2024, 1, 15)).run()
    assert not app.exception
    assert app.metric[4].value == "5"
    app.button(key="return_home_visualization").click().run()
    app.button(key="home_enter_visualization").click().run()
    assert app.session_state["visualization_dates"] == (date(2024, 1, 9), date(2024, 1, 15))
    assert app.multiselect(key="visualization_ma").value == [5]
    assert app.multiselect(key="visualization_charts").value == ["K线", "成交量"]
    app.button(key="visualization_backtest").click().run()
    assert app.session_state["app_page"] == "backtest"
    assert "自定义策略回测" in [item.value for item in app.title]


def test_acquisition_selection_is_shared_and_restored(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    app = open_visualization(tmp_path, monkeypatch)
    app.date_input(key="visualization_dates_start").set_value(date(2024, 1, 9))
    app.date_input(key="visualization_dates_end").set_value(date(2024, 1, 15)).run()
    app.button(key="return_home_visualization").click().run()
    app.button(key="home_enter_data_acquisition").click().run()
    assert app.session_state["stock_selected"]["symbol"] == "600519"
    assert app.session_state["stock_history_dates"] == (date(2024, 1, 9), date(2024, 1, 15))
    app.button(key="stock_open_visualization").click().run()
    assert app.session_state["app_page"] == "visualization"
    assert app.metric[4].value == "5"


def test_missing_range_and_invalid_cache_are_user_messages(tmp_path, monkeypatch):
    seed_cache(tmp_path)
    app = open_visualization(tmp_path, monkeypatch)
    app.date_input(key="visualization_dates_start").set_value(date(2025, 1, 1))
    app.date_input(key="visualization_dates_end").set_value(date(2025, 2, 1)).run()
    assert any("没有缓存行情" in item.value for item in app.info)
    assert not app.exception
    bad = seed_cache(tmp_path)
    bad.loc[2, "close"] = -1
    save_history_cache(bad, {"coverage_ranges": [{"start": "2024-01-02", "end": "2024-02-12"}]}, "600519", data_dir=tmp_path)
    app.date_input(key="visualization_dates_start").set_value(date(2024, 1, 2))
    app.date_input(key="visualization_dates_end").set_value(date(2024, 2, 12)).run()
    assert any("暂时无法计算" in item.value for item in app.error)
    assert not app.exception


def test_etf_cached_page_shows_volume_in_units_without_network(tmp_path, monkeypatch):
    seed_cache(tmp_path, asset_type="ETF", symbol="159516")
    app = open_visualization(tmp_path, monkeypatch)
    assert app.session_state["research_context"]["asset_type"] == "ETF"
    assert len(app.get("plotly_chart")) == 4
    assert not app.exception


