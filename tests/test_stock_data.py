from datetime import date

import pandas as pd
import pytest

from src.stock_data.cache import load_history_cache, missing_date_ranges
from src.stock_data.errors import StockInputError, StockProviderError, StockSchemaError
from src.stock_data.provider import fetch_stock_history, fetch_tencent_history, normalize_history
from src.stock_data.service import get_stock_history, read_stock_history_cache, validate_request
from src.stock_data.symbols import normalize_stock_universe, search_stocks


def raw_history(days=("2024-01-02", "2024-01-03")):
    return pd.DataFrame({"日期": list(days), "开盘": [10, 11][:len(days)], "最高": [12, 13][:len(days)], "最低": [9, 10][:len(days)], "收盘": [11, 12][:len(days)], "成交量": [1000, 1200][:len(days)], "成交额": [10000, 14000][:len(days)]})


def test_validate_code_dates_and_adjustment():
    assert validate_request("600519", date(2024, 1, 1), date(2024, 1, 2)) == ("600519", "2024-01-01", "2024-01-02")
    for args in (("abc", "2024-01-01", "2024-01-02", "qfq"), ("9999999", "2024-01-01", "2024-01-02", "qfq"), ("600519", "2024-01-03", "2024-01-02", "qfq"), ("600519", "2024-01-01", "2024-01-02", "bad")):
        with pytest.raises(StockInputError):
            validate_request(*args)


def test_search_code_and_chinese_name_requires_exact_user_selection():
    universe = normalize_stock_universe(pd.DataFrame({"code": ["600519", "000858", "300750"], "name": ["贵州茅台", "五粮液", "宁德时代"]}))
    assert search_stocks("600519", universe).iloc[0]["name"] == "贵州茅台"
    assert search_stocks("茅台", universe).iloc[0]["symbol"] == "600519"
    assert search_stocks("不存在", universe).empty
    assert search_stocks("60051", universe).empty


def test_normalization_types_optional_amount_and_missing_fields():
    frame = normalize_history(raw_history(), symbol="600519", name="贵州茅台", market="上海", adjustment="qfq")
    assert list(frame.columns[:7]) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert pd.api.types.is_string_dtype(frame["date"])
    assert pd.api.types.is_integer_dtype(frame["volume"])
    assert frame.iloc[0]["symbol"] == "600519"
    with pytest.raises(StockSchemaError, match="缺少必要字段"):
        normalize_history(pd.DataFrame({"日期": ["2024-01-02"], "开盘": [1]}), symbol="600519", name="", market="上海", adjustment="qfq")
    with pytest.raises(StockSchemaError, match="无法解析"):
        normalize_history(pd.DataFrame({"日期": ["2024-01-02"], "开盘": ["bad"], "最高": [2], "最低": [1], "收盘": [2], "成交量": [1]}), symbol="600519", name="", market="上海", adjustment="qfq")


def test_first_fetch_then_second_request_hits_cache(tmp_path):
    calls = []
    def fetcher(**kwargs):
        calls.append(kwargs)
        return raw_history()
    first = get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=fetcher)
    second = get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: pytest.fail("cache miss"))
    assert first.status == "LIVE_FETCHED"
    assert second.status == "CACHE"
    assert len(calls) == 1
    assert first.metadata["source"] == "Tencent"
    assert first.metadata["coverage_ranges"] == [{"start": "2024-01-01", "end": "2024-01-04"}]


def test_expanded_interval_requests_only_uncovered_dates(tmp_path):
    requests = []
    def fetcher(**kwargs):
        requests.append((kwargs["start_date"], kwargs["end_date"]))
        return raw_history(("2024-01-05",))
    get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    result = get_stock_history("600519", "2024-01-01", "2024-01-06", data_dir=tmp_path, fetcher=fetcher)
    assert requests == [("2024-01-05", "2024-01-06")]
    assert result.status == "LIVE_FETCHED"
    assert result.metadata["record_count"] == 3


def test_failure_preserves_cache_and_marks_stale(tmp_path):
    get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    before, _ = load_history_cache("600519", data_dir=tmp_path)
    result = get_stock_history("600519", "2024-01-01", "2024-01-10", data_dir=tmp_path, fetcher=lambda **kwargs: (_ for _ in ()).throw(TimeoutError("offline")))
    after, _ = load_history_cache("600519", data_dir=tmp_path)
    assert result.status == "STALE_CACHE"
    assert "原缓存未改动" in result.message
    pd.testing.assert_frame_equal(before, after)


def test_overlapping_refresh_deduplicates_by_date(tmp_path):
    get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    result = get_stock_history("600519", "2024-01-01", "2024-01-04", force_refresh=True, data_dir=tmp_path, fetcher=lambda **kwargs: raw_history(("2024-01-02", "2024-01-04")))
    assert result.data["date"].is_unique
    assert len(result.data) == 3


def test_adjustment_modes_have_separate_cache(tmp_path):
    get_stock_history("600519", "2024-01-01", "2024-01-04", adjustment="qfq", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    hfq = get_stock_history("600519", "2024-01-01", "2024-01-04", adjustment="hfq", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    assert hfq.status == "LIVE_FETCHED"
    assert read_stock_history_cache("600519", "2024-01-01", "2024-01-04", adjustment="qfq", data_dir=tmp_path).status == "CACHE"


def test_weekend_only_interval_is_successfully_covered(tmp_path):
    result = get_stock_history("600519", "2024-01-06", "2024-01-07", data_dir=tmp_path, fetcher=lambda **kwargs: pd.DataFrame())
    assert result.status == "LIVE_FETCHED"
    assert result.data.empty
    assert read_stock_history_cache("600519", "2024-01-06", "2024-01-07", data_dir=tmp_path).status == "CACHE"


def test_missing_range_calculation_merges_overlap_and_adjacent_intervals():
    assert missing_date_ranges("2024-01-01", "2024-01-10", [{"start": "2024-01-03", "end": "2024-01-05"}, {"start": "2024-01-06", "end": "2024-01-07"}]) == [("2024-01-01", "2024-01-02"), ("2024-01-08", "2024-01-10")]


def test_file_write_failure_keeps_previous_cache(tmp_path, monkeypatch):
    import src.stock_data.cache as cache

    get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    before, metadata_before = load_history_cache("600519", data_dir=tmp_path)
    monkeypatch.setattr(cache, "atomic_write_csv", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    result = get_stock_history("600519", "2024-01-01", "2024-01-10", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    after, metadata_after = load_history_cache("600519", data_dir=tmp_path)
    assert result.status == "STALE_CACHE"
    pd.testing.assert_frame_equal(before, after)
    assert metadata_before == metadata_after


def test_metadata_write_failure_rolls_back_new_csv(tmp_path, monkeypatch):
    import src.stock_data.cache as cache

    get_stock_history("600519", "2024-01-01", "2024-01-04", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    before, metadata_before = load_history_cache("600519", data_dir=tmp_path)
    monkeypatch.setattr(cache, "atomic_write_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    result = get_stock_history("600519", "2024-01-01", "2024-01-10", data_dir=tmp_path, fetcher=lambda **kwargs: raw_history())
    after, metadata_after = load_history_cache("600519", data_dir=tmp_path)
    assert result.status == "STALE_CACHE"
    pd.testing.assert_frame_equal(before, after)
    assert metadata_before == metadata_after


def test_stock_universe_cache_prevents_repeated_fetch(tmp_path):
    from src.stock_data.symbols import get_stock_universe

    raw = pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]})
    first, _ = get_stock_universe(data_dir=tmp_path, allow_fetch_if_missing=True, fetcher=lambda: raw)
    second, _ = get_stock_universe(data_dir=tmp_path, allow_fetch_if_missing=True, fetcher=lambda: pytest.fail("should use local list"))
    assert first.to_dict("records") == second.to_dict("records")


def test_etf_search_normalizes_exchange_and_finds_code_or_name():
    from src.stock_data.symbols import normalize_etf_universe

    etfs = normalize_etf_universe(pd.DataFrame({"代码": ["159516", "510300", "600519"], "名称": ["样例ETF", "沪深300ETF", "不应保留"]}))
    assert search_stocks("159516", etfs).iloc[0]["name"] == "样例ETF"
    assert search_stocks("沪深300", etfs).iloc[0]["symbol"] == "510300"
    assert etfs.set_index("symbol").loc["159516", "market"] == "深圳"
    assert etfs.set_index("symbol").loc["510300", "market"] == "上海"
    assert "600519" not in etfs["symbol"].tolist()


def test_etf_universe_is_cached_separately(tmp_path):
    from src.stock_data.symbols import get_etf_universe

    raw = pd.DataFrame({"代码": ["159516"], "名称": ["样例ETF"]})
    first, meta = get_etf_universe(data_dir=tmp_path, allow_fetch_if_missing=True, fetcher=lambda: raw)
    second, _ = get_etf_universe(data_dir=tmp_path, allow_fetch_if_missing=True, fetcher=lambda: pytest.fail("should use ETF cache"))
    assert first.to_dict("records") == second.to_dict("records")
    assert meta["api_name"] == "injected_fetcher"
    assert (tmp_path / "etf_universe.csv").exists()
    assert (tmp_path / "universe.csv").exists() is False


def test_etf_catalog_merges_multiple_sources_and_records_partial_failures():
    from src.stock_data.symbols import fetch_etf_universe_from_sources

    class MockAK:
        @staticmethod
        def fund_etf_category_sina(symbol):
            assert symbol == "ETF基金"
            return pd.DataFrame({"代码": ["510300"], "名称": ["沪深300ETF"]})

        @staticmethod
        def fund_etf_category_ths(symbol, date):
            assert symbol == "ETF" and date == ""
            return pd.DataFrame({"基金代码": ["159516"], "基金名称": ["国泰中证半导体材料设备主题ETF"]})

    frame, sources, errors = fetch_etf_universe_from_sources(MockAK)
    assert set(frame["symbol"]) == {"159516", "510300"}
    assert sources == ["fund_etf_category_sina", "fund_etf_category_ths"]
    assert errors == []


def test_etf_history_uses_tencent_and_cache_namespace(tmp_path):
    call = {}
    def etf_fetcher(**kwargs):
        call.update(kwargs)
        return raw_history()
    result = get_stock_history("159516", "2024-01-01", "2024-01-04", asset_type="ETF", adjustment="qfq", name="样例ETF", market="深圳", data_dir=tmp_path, fetcher=etf_fetcher)
    assert result.status == "LIVE_FETCHED"
    assert result.data.iloc[0]["asset_type"] == "ETF"
    assert result.data.iloc[0]["provider"] == "tencent.fqkline.get"
    assert result.data.iloc[0]["source"] == "Tencent"
    assert call == {"symbol": "159516", "start_date": "2024-01-01", "end_date": "2024-01-04", "adjustment": "qfq", "market": "深圳", "asset_type": "ETF"}
    assert (tmp_path / "history" / "etf" / "daily" / "qfq" / "159516.csv").exists()
    assert read_stock_history_cache("159516", "2024-01-01", "2024-01-04", adjustment="qfq", asset_type="ETF", data_dir=tmp_path).status == "CACHE"


def test_etf_history_provider_call_and_bilingual_ui_labels_are_compatible():
    from src.stock_data.page import FIELD_DISPLAY_NAMES

    args = {}
    result = fetch_stock_history("159516", "2024-01-01", "2024-01-04", asset_type="ETF", adjustment="hfq", market="深圳", fetcher=lambda **kwargs: (args.update(kwargs) or raw_history()))
    assert args == {"symbol": "159516", "start_date": "2024-01-01", "end_date": "2024-01-04", "adjustment": "hfq", "market": "深圳", "asset_type": "ETF"}
    assert result.iloc[0]["provider"] == "tencent.fqkline.get"
    assert result.iloc[0]["source"] == "Tencent"
    assert result.iloc[0]["adjustment"] == "hfq"
    assert FIELD_DISPLAY_NAMES["date"] == "date（日期）"
    assert FIELD_DISPLAY_NAMES["volume"] == "volume（成交量）"


def test_tencent_unadjusted_and_beijing_requests_fail_explicitly():
    with pytest.raises(StockProviderError, match="不复权日线暂不可用"):
        fetch_stock_history("159516", "2024-01-01", "2024-01-04", asset_type="ETF", adjustment="", market="深圳", fetcher=lambda **kwargs: raw_history())
    with pytest.raises(StockProviderError, match="北交所"):
        fetch_stock_history("920002", "2024-01-01", "2024-01-04", asset_type="A股", adjustment="qfq", market="北京", fetcher=lambda **kwargs: raw_history())


def test_etf_tencent_history_is_sliced_and_cached_for_requested_dates(tmp_path):
    calls = []
    def fetcher(**kwargs):
        calls.append(kwargs)
        return raw_history(("2024-01-02", "2024-01-03"))

    result = get_stock_history(
        "159516", "2024-01-03", "2024-01-03", asset_type="ETF", adjustment="qfq",
        market="深圳", data_dir=tmp_path, fetcher=fetcher,
    )
    assert calls == [{
        "symbol": "159516",
        "start_date": "2024-01-03",
        "end_date": "2024-01-03",
        "adjustment": "qfq",
        "market": "深圳",
        "asset_type": "ETF",
    }]
    assert result.status == "LIVE_FETCHED"
    assert result.data["date"].tolist() == ["2024-01-03"]
    cached = read_stock_history_cache(
        "159516", "2024-01-03", "2024-01-03", asset_type="ETF", adjustment="qfq", data_dir=tmp_path,
    )
    assert cached.status == "CACHE"
    assert cached.data["date"].tolist() == ["2024-01-03"]


def test_tencent_provider_parses_adjusted_rows_with_mocked_http():
    from src.stock_data.provider import fetch_tencent_history

    requests_seen = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"sz159516": {"qfqday": [["2024-01-02", "1.0", "1.2", "1.3", "0.9", "123"]]}}}

    def fake_get(url, *, params, timeout):
        requests_seen.append((url, params, timeout))
        return FakeResponse()

    result = fetch_tencent_history(
        "159516", "2024-01-01", "2024-01-03", adjustment="qfq", market="深圳", http_get=fake_get,
    )

    assert len(requests_seen) == 1
    assert requests_seen[0][1]["param"] == "sz159516,day,2024-01-01,2024-01-03,640,qfq"
    assert result.to_dict("records") == [{
        "date": "2024-01-02", "open": "1.0", "close": "1.2", "high": "1.3", "low": "0.9", "volume": "123",
        "price_series": "qfqday",
    }]


def test_tencent_normalization_keeps_missing_optional_amount_as_null():
    result = fetch_stock_history(
        "159516", "2024-01-02", "2024-01-02", asset_type="ETF", adjustment="qfq", market="深圳",
        fetcher=lambda **kwargs: pd.DataFrame({
            "date": ["2024-01-02"], "open": [1.0], "high": [1.3], "low": [0.9], "close": [1.2], "volume": [123],
        }),
    )

    assert result.loc[0, "amount"] != result.loc[0, "amount"]  # NaN, because the provider omits turnover amount.
