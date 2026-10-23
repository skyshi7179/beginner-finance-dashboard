"""Offline checks for cross-source units, provenance and cache safety."""

import pandas as pd
import pytest

from src.stock_data.cache import cache_paths, load_history_cache
from src.stock_data.errors import StockProviderError, StockSchemaError
from src.stock_data.provider import fetch_stock_history
from src.stock_data.service import get_stock_history, read_stock_history_cache
from src.stock_data.turnover import fetch_sina_turnover, normalize_turnover


def prices(**kwargs):
    # Tencent lots. Adjusted prices deliberately differ from raw Sina prices.
    return pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "open": [20, 22],
                         "high": [24, 26], "low": [18, 20], "close": [22, 24], "volume": [10, 12]})


def turnover(*args, **kwargs):
    return pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "volume": [1020, 1210],
                         "amount": [11220, 14520], "low": [9, 10], "high": [12, 13]})


@pytest.mark.parametrize("symbol,market,asset_type,unit", [("600519", "上海", "A股", "股"), ("159516", "深圳", "ETF", "份")])
@pytest.mark.parametrize("adjustment", ["qfq", "hfq"])
def test_merge_retains_adjusted_prices_and_uses_raw_turnover(symbol, market, asset_type, unit, adjustment):
    result = fetch_stock_history(symbol, "2024-01-02", "2024-01-03", adjustment=adjustment,
                                 market=market, asset_type=asset_type, fetcher=prices, turnover_fetcher=turnover)
    assert result["close"].tolist() == [22, 24]
    assert result["volume"].tolist() == [1020, 1210]
    assert result["amount"].tolist() == [11220, 14520]
    assert result["volume_unit"].eq(unit).all()
    assert result["amount_unit"].eq("元").all()
    assert result["source"].eq("Tencent").all()
    assert result["volume_source"].eq("sina.klc_kl").all()
    assert result["amount_source"].eq("sina.klc_kl").all()
    assert result["turnover_status"].eq("COMPLETE").all()


def test_missing_dates_use_left_join_and_conflicts_do_not_overwrite():
    def conflict(*args, **kwargs):
        return pd.DataFrame({"date": ["2024-01-02", "2024-01-04"], "volume": [3000, 1000], "amount": [33000, 11000]})

    result = fetch_stock_history("159516", "2024-01-02", "2024-01-04", asset_type="ETF", market="深圳", fetcher=prices, turnover_fetcher=conflict)
    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert result["volume"].tolist() == [1000, 1200]
    assert result["amount"].isna().all()
    assert result["turnover_status"].tolist() == ["CONFLICT", "PARTIAL"]
    assert result["amount_source"].eq("").all()


def test_sina_failure_keeps_prices_and_marks_failed_enrichment():
    def offline(*args, **kwargs):
        raise TimeoutError("offline")

    result = fetch_stock_history("600519", "2024-01-02", "2024-01-03", fetcher=prices, turnover_fetcher=offline)
    assert result["close"].tolist() == [22, 24]
    assert result["volume"].tolist() == [1000, 1200]
    assert result["amount"].isna().all()
    assert result["turnover_status"].eq("ERROR").all()
    assert result["turnover_message"].str.contains("offline").all()


def test_sina_zero_placeholder_is_not_accepted_even_within_rounding_tolerance():
    def one_lot(**kwargs):
        return prices().iloc[:1].assign(volume=1)

    def zero_placeholder(*args, **kwargs):
        return pd.DataFrame({"date": ["2024-01-02"], "volume": [0], "amount": [0]})

    result = fetch_stock_history("159516", "2024-01-02", "2024-01-02", asset_type="ETF",
                                 fetcher=one_lot, turnover_fetcher=zero_placeholder)
    assert result.loc[0, "volume"] == 100
    assert pd.isna(result.loc[0, "amount"])
    assert result.loc[0, "turnover_status"] == "CONFLICT"


@pytest.mark.parametrize("mutate", [
    lambda df: df.drop(columns="amount"),
    lambda df: df.assign(volume="bad"),
    lambda df: df.assign(amount=-1),
    lambda df: df.assign(amount=float("inf")),
    lambda df: df.assign(volume=1.2),
    lambda df: df.assign(amount=0),
    lambda df: df.assign(amount=df["amount"] * 10000),
    lambda df: df.assign(date="bad"),
])
def test_sina_rejects_schema_type_and_unit_changes(mutate):
    with pytest.raises(StockSchemaError):
        normalize_turnover(mutate(turnover()), "2024-01-02", "2024-01-03")


def test_sina_deduplicates_identical_dates_but_rejects_conflicting_values():
    raw = turnover()
    assert len(normalize_turnover(pd.concat([raw, raw]), "2024-01-02", "2024-01-03")) == 2
    with pytest.raises(StockSchemaError, match="冲突"):
        normalize_turnover(pd.concat([raw, raw.assign(amount=raw["amount"] + 1)]), "2024-01-02", "2024-01-03")


def test_http_adapter_slices_full_history_and_uses_only_sina():
    calls = []

    class Response:
        text = "encoded history fixture"

        def raise_for_status(self):
            pass

    def http_get(url, *, timeout):
        calls.append((url, timeout))
        return Response()

    result = fetch_sina_turnover("159516", "2024-01-03", "2024-01-03", market="深圳", http_get=http_get,
                                decoder=lambda text: turnover().to_dict("records"))
    assert result["date"].tolist() == ["2024-01-03"]
    assert calls == [("https://finance.sina.com.cn/realstock/company/sz159516/hisdata_klc2/klc_kl.js", 20)]


def test_http_adapter_reports_timeout():
    def offline(*args, **kwargs):
        raise TimeoutError("timeout")

    with pytest.raises(StockProviderError, match="timeout"):
        fetch_sina_turnover("159516", "2024-01-02", "2024-01-03", market="深圳", http_get=offline)


def test_cache_hit_does_not_repeat_either_provider_and_preserves_provenance(tmp_path):
    first = get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                              fetcher=prices, turnover_fetcher=turnover)

    def unexpected(*args, **kwargs):
        pytest.fail("cache hit must not call a provider")

    second = get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                               fetcher=unexpected, turnover_fetcher=unexpected)
    assert second.status == "CACHE"
    assert second.metadata["turnover_status"] == "COMPLETE"
    assert second.metadata["amount_sources"] == ["sina.klc_kl"]
    pd.testing.assert_frame_equal(first.data, second.data, check_dtype=False)


def test_failed_supplement_refresh_preserves_old_turnover_as_stale(tmp_path):
    first = get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                              fetcher=prices, turnover_fetcher=turnover)

    def offline(*args, **kwargs):
        raise TimeoutError("offline")

    refreshed = get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                                  force_refresh=True, fetcher=prices, turnover_fetcher=offline)
    assert refreshed.data["amount"].tolist() == first.data["amount"].tolist()
    assert refreshed.data["volume"].tolist() == first.data["volume"].tolist()
    assert refreshed.data["turnover_fetched_at"].tolist() == first.data["turnover_fetched_at"].tolist()
    assert refreshed.data["turnover_status"].eq("STALE").all()
    assert refreshed.metadata["turnover_missing_rows"] == 2
    assert "成交字段待核验" in refreshed.message
    assert "不表示缺少交易日" in refreshed.message
    cached, _ = load_history_cache("159516", data_dir=tmp_path, asset_type="ETF")
    assert cached["amount"].tolist() == first.data["amount"].tolist()


def test_legacy_cache_does_not_guess_units_and_refresh_replaces_selected_rows(tmp_path):
    get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                      fetcher=prices, turnover_fetcher=turnover)
    csv, _ = cache_paths("159516", data_dir=tmp_path, asset_type="ETF")
    raw = pd.read_csv(csv)
    raw = raw.drop(columns=[c for c in raw.columns if c.startswith("turnover_") or c in {"volume_unit", "amount_unit", "volume_source", "amount_source"}])
    raw.to_csv(csv, index=False)
    legacy = read_stock_history_cache("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path)
    assert legacy.data["volume_unit"].eq("UNKNOWN").all()
    assert legacy.data["turnover_status"].eq("LEGACY").all()
    refreshed = get_stock_history("159516", "2024-01-02", "2024-01-02", asset_type="ETF", data_dir=tmp_path,
                                  force_refresh=True, fetcher=prices, turnover_fetcher=turnover)
    assert refreshed.data["turnover_status"].eq("COMPLETE").all()
    rest = read_stock_history_cache("159516", "2024-01-03", "2024-01-03", asset_type="ETF", data_dir=tmp_path)
    assert rest.data["turnover_status"].eq("LEGACY").all()


def test_page_fetch_displays_turnover_and_rerun_does_not_download_again(tmp_path, monkeypatch):
    from datetime import date
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import src.stock_data.page as page
    from src.stock_data.service import StockHistoryResult

    result = get_stock_history("159516", "2024-01-02", "2024-01-03", asset_type="ETF", data_dir=tmp_path,
                               fetcher=prices, turnover_fetcher=turnover)
    calls = []

    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(page, "get_stock_history", fetch)
    monkeypatch.setattr(page, "read_stock_history_cache", lambda *args, **kwargs: StockHistoryResult(pd.DataFrame(), "CACHE_MISS", {}))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    app.session_state["stock_search_asset_type"] = "ETF"
    app.session_state["stock_selected"] = {"symbol": "159516", "name": "测试ETF", "market": "深圳"}
    app.session_state["stock_selected_type"] = "ETF"
    app.session_state["stock_history_dates"] = (date(2024, 1, 2), date(2024, 1, 3))
    app.button(key="home_enter_data_acquisition").click().run()
    assert calls == []
    app.button(key="stock_fetch").click().run()
    assert not app.exception
    assert len(calls) == 1
    assert any("成交量单位：份" in item.value for item in app.caption)
    assert app.dataframe[0].value["amount（成交额，元）"].tolist() == [11220, 14520]
    app.run()
    assert not app.exception
    assert len(calls) == 1
