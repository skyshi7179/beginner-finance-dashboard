"""Regression checks for Tencent's raw-day response to adjusted requests."""

import pandas as pd
import pytest

from src.stock_data.errors import StockProviderError, StockSchemaError
from src.stock_data.provider import fetch_tencent_history
from src.stock_data.service import get_stock_history, read_stock_history_cache


ROW = ["2026-10-13", "45.000", "38.800", "45.000", "38.000", "1711578.000"]


def response_get(security, code=0, tencent_code="sz301660"):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": code, "data": {tencent_code: security}}

    return lambda *args, **kwargs: Response()


@pytest.mark.parametrize("adjustment", ["qfq", "hfq"])
def test_adjusted_request_accepts_day_but_keeps_actual_series(adjustment):
    result = fetch_tencent_history("301660", "2025-10-20", "2026-10-19", adjustment=adjustment, market="深圳",
                                   http_get=response_get({"day": [ROW]}))
    assert result["price_series"].tolist() == ["day"]
    assert result["close"].tolist() == ["38.800"]


@pytest.mark.parametrize("adjustment", ["qfq", "hfq"])
def test_requested_adjusted_series_has_priority_over_day(adjustment):
    adjusted_row = [*ROW]
    adjusted_row[2] = "99.000"
    result = fetch_tencent_history("301660", "2026-10-13", "2026-10-19", adjustment=adjustment, market="深圳",
                                   http_get=response_get({"day": [ROW], f"{adjustment}day": [adjusted_row]}))
    assert result["price_series"].tolist() == [f"{adjustment}day"]
    assert result["close"].tolist() == ["99.000"]


def test_wrong_adjustment_is_not_used_as_fallback():
    with pytest.raises(StockProviderError, match="没有返回"):
        fetch_tencent_history("301660", "2026-10-13", "2026-10-19", adjustment="qfq", market="深圳",
                              http_get=response_get({"hfqday": [ROW]}))


def test_error_response_is_not_accepted_even_if_day_is_present():
    with pytest.raises(StockProviderError, match="错误响应"):
        fetch_tencent_history("301660", "2026-10-13", "2026-10-19", adjustment="qfq", market="深圳",
                              http_get=response_get({"day": [ROW]}, code=-1))


def test_malformed_series_is_reported_as_schema_error():
    with pytest.raises(StockSchemaError, match="列表"):
        fetch_tencent_history("301660", "2026-10-13", "2026-10-19", adjustment="qfq", market="深圳",
                              http_get=response_get({"qfqday": None, "day": [ROW]}))


def test_600519_hfq_accepts_mixed_rows_with_corporate_action_metadata():
    dividend = {
        "nd": "2023",
        "fh_sh": "308.76",
        "djr": "2024-06-18",
        "cqr": "2024-06-19",
        "FHcontent": "10派308.76元",
    }
    ordinary = ["2024-06-18", "9720.000", "9700.000", "9750.000", "9680.000", "30000.000"]
    ex_dividend = ["2024-06-19", "9651.387", "9668.327", "9685.210", "9561.962", "41262.000", dividend]

    result = fetch_tencent_history(
        "600519",
        "2024-06-18",
        "2024-06-19",
        adjustment="hfq",
        market="上海",
        http_get=response_get({"hfqday": [ordinary, ex_dividend]}, tencent_code="sh600519"),
    )

    assert result["date"].tolist() == ["2024-06-18", "2024-06-19"]
    assert result["close"].tolist() == ["9700.000", "9668.327"]
    assert result["price_series"].tolist() == ["hfqday", "hfqday"]
    assert result.columns.tolist() == ["date", "open", "close", "high", "low", "volume", "price_series"]


@pytest.mark.parametrize(
    "bad_row, expected_message",
    [
        (["2024-06-19", "1", "1", "1", "1"], "包含 5 个字段"),
        (["2024-06-19", "1", "1", "1", "1", "1", "unexpected"], "第 7 个字段"),
    ],
)
def test_unrecognized_tencent_row_shapes_remain_explicit_schema_errors(bad_row, expected_message):
    with pytest.raises(StockSchemaError, match=expected_message):
        fetch_tencent_history(
            "600519",
            "2024-06-19",
            "2024-06-19",
            adjustment="hfq",
            market="上海",
            http_get=response_get({"hfqday": [bad_row]}, tencent_code="sh600519"),
        )


@pytest.mark.parametrize("adjustment", ["qfq", "hfq"])
def test_raw_day_cache_retains_requested_and_actual_adjustments_separately(tmp_path, adjustment):
    def prices(**kwargs):
        return fetch_tencent_history("301660", "2025-10-20", "2026-10-19", adjustment=adjustment, market="深圳",
                                     http_get=response_get({"day": [ROW]}))

    def turnover(*args, **kwargs):
        return pd.DataFrame({"date": ["2026-10-13"], "volume": [171157800], "amount": [6800000000]})

    first = get_stock_history("301660", "2025-10-20", "2026-10-19", adjustment=adjustment, market="深圳",
                              data_dir=tmp_path, fetcher=prices, turnover_fetcher=turnover)
    assert first.status == "LIVE_FETCHED"
    assert first.data["adjustment"].tolist() == ["none"]
    assert first.data["requested_adjustment"].tolist() == [adjustment]
    assert first.metadata["actual_adjustments"] == ["none"]
    assert first.metadata["price_fallback_rows"] == 1
    assert first.metadata["turnover_status"] == "COMPLETE"
    assert "原始日线" in first.message
    cached = read_stock_history_cache("301660", "2025-10-20", "2026-10-19", adjustment=adjustment, data_dir=tmp_path)
    assert cached.status == "CACHE"
    assert cached.data["price_series"].tolist() == ["day"]
    assert cached.data["adjustment"].tolist() == ["none"]
    assert cached.data["requested_adjustment"].tolist() == [adjustment]
    assert "原始日线" in cached.message


def test_page_warns_about_raw_day_and_shows_actual_adjustment(tmp_path, monkeypatch):
    from datetime import date
    from streamlit.testing.v1 import AppTest
    import src.stock_data.page as page

    def prices(**kwargs):
        return fetch_tencent_history("301660", "2025-10-20", "2026-10-19", adjustment="qfq", market="深圳",
                                     http_get=response_get({"day": [ROW]}))

    def turnover(*args, **kwargs):
        return pd.DataFrame({"date": ["2026-10-13"], "volume": [171157800], "amount": [6800000000]})

    result = get_stock_history("301660", "2025-10-20", "2026-10-19", market="深圳", data_dir=tmp_path,
                               fetcher=prices, turnover_fetcher=turnover)

    def unexpected(*args, **kwargs):
        pytest.fail("page rerun must not fetch")

    monkeypatch.setattr(page, "get_stock_history", unexpected)
    monkeypatch.setattr(page, "read_stock_history_cache", unexpected)

    def script():
        from src.stock_data.page import render_stock_data_page
        render_stock_data_page()

    app = AppTest.from_function(script, default_timeout=30)
    app.session_state["stock_selected"] = {"symbol": "301660", "name": "粤芯半导体", "market": "深圳"}
    app.session_state["stock_selected_type"] = "A股"
    app.session_state["stock_history_dates"] = (date(2025, 10, 20), date(2026, 10, 19))
    app.session_state["stock_history_result"] = result
    app.session_state["stock_history_signature"] = ("A股", "301660", "2025-10-20", "2026-10-19", "qfq")
    app.run()
    assert not app.exception
    assert any("原始日线 day" in warning.value for warning in app.warning)
    assert app.dataframe[0].value["adjustment（实际复权）"].tolist() == ["none"]
