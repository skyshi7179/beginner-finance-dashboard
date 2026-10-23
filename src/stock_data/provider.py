"""Tencent K-line adapter and canonical normalization for A-share/ETF history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd
import requests

from src.stock_data.errors import StockProviderError, StockSchemaError
from src.stock_data.turnover import enrich_turnover, fetch_sina_turnover, normalize_turnover


CANONICAL_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
PRICE_COLUMNS = ["requested_adjustment", "price_series"]
ALIASES = {
    "date": ("date", "日期", "时间"),
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "成交量"),
    "amount": ("amount", "成交额"),
}


PAGE_SIZE = 640
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_KLINE_COLUMNS = ["date", "open", "close", "high", "low", "volume"]


def _tencent_market_code(symbol: str, market: str) -> str:
    if market == "北京" or symbol.startswith(("4", "8", "92")):
        raise StockProviderError("腾讯复权 K 线接口当前未返回北交所证券数据；该证券无法通过当前统一数据源获取。")
    if market == "深圳" or symbol.startswith(("0", "1", "2", "3")):
        return f"sz{symbol}"
    if market == "上海" or symbol.startswith(("5", "6", "9")):
        return f"sh{symbol}"
    raise StockProviderError(f"无法根据证券代码判断腾讯行情市场：{symbol}。")


def _parse_tencent_kline_rows(rows: list, series_name: str) -> pd.DataFrame:
    """Parse Tencent rows with optional ex-rights/dividend metadata.

    Ordinary rows contain six OHLCV fields. On corporate-action dates Tencent
    appends a seventh mapping such as ``{"FHcontent": "10派...元"}``. That
    mapping describes the adjustment event and is not another numeric market
    data column, so the canonical price adapter deliberately excludes it.
    """
    parsed_rows: list[list] = []
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, (list, tuple)):
            raise StockSchemaError(f"腾讯 {series_name} 第 {row_number} 行不是 K 线列表。")
        if len(row) == 7:
            if row[6] is not None and not isinstance(row[6], dict):
                raise StockSchemaError(
                    f"腾讯 {series_name} 第 {row_number} 行的第 7 个字段不是可识别的除权除息说明。"
                )
        elif len(row) != 6:
            raise StockSchemaError(
                f"腾讯 {series_name} 第 {row_number} 行包含 {len(row)} 个字段；"
                "预期 6 个行情字段，或 7 个（含除权除息说明）。"
            )
        parsed_rows.append(list(row[:6]))
    return pd.DataFrame(parsed_rows, columns=TENCENT_KLINE_COLUMNS)


def fetch_tencent_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjustment: str,
    market: str,
    http_get: Callable = requests.get,
) -> pd.DataFrame:
    """Fetch Tencent's qfq/hfq daily series, paging backward when needed."""
    if adjustment not in {"qfq", "hfq"}:
        raise StockProviderError("腾讯 K 线接口当前仅确认支持前复权和后复权；不复权日线不可用。")
    tencent_code = _tencent_market_code(symbol, market)
    series_key = f"{adjustment}day"
    cursor = end_date
    pages: list[pd.DataFrame] = []
    page_count = 0
    while cursor >= start_date:
        page_count += 1
        if page_count > 100:
            raise StockProviderError("历史分页超过安全上限，未提交不完整下载，请缩小区间。")
        param = f"{tencent_code},day,{start_date},{cursor},{PAGE_SIZE},{adjustment}"
        try:
            response = http_get(TENCENT_KLINE_URL, params={"param": param}, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise StockProviderError(f"腾讯行情请求失败（网络、限流或接口错误）：{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            raise StockProviderError(f"腾讯接口返回错误响应：{payload!r}")
        security = (payload.get("data") or {}).get(tencent_code)
        if not isinstance(security, dict):
            raise StockProviderError(f"腾讯接口没有返回 {symbol} 的 {series_key} 数据；请检查证券代码或市场是否受支持。")
        # Tencent can return `day` even when qfq/hfq was requested (e.g. new
        # listings). Keep its real series name; never substitute the opposite
        # adjustment mode or silently label raw prices as adjusted prices.
        actual_series = series_key if series_key in security else "day" if "day" in security else None
        if actual_series is None:
            raise StockProviderError(f"腾讯接口没有返回 {symbol} 的 {series_key} 或 day 数据；实际字段：{', '.join(security)}。")
        rows = security[actual_series]
        if not isinstance(rows, list):
            raise StockSchemaError(f"腾讯 {actual_series} 必须返回 K 线列表。")
        if not rows:
            break
        page = _parse_tencent_kline_rows(rows, actual_series)
        page["price_series"] = actual_series
        pages.append(page)
        earliest = pd.to_datetime(page["date"], errors="coerce").min()
        if pd.isna(earliest):
            raise StockSchemaError("历史分页日期无效，未提交不完整下载。")
        if earliest.date().isoformat() > cursor:
            raise StockProviderError("历史分页未向过去推进，数据源可能忽略日期参数；未提交不完整下载。")
        if earliest.date().isoformat() <= start_date or len(rows) < PAGE_SIZE:
            break
        cursor = (earliest.date() - timedelta(days=1)).isoformat()
    if not pages:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "price_series"])
    result = pd.concat(pages, ignore_index=True)
    return result[result["date"].between(start_date, end_date)].drop_duplicates("date").sort_values("date").reset_index(drop=True)


def price_metadata(frame: pd.DataFrame) -> dict:
    return {
        "price_series": sorted(frame["price_series"].dropna().unique().tolist()),
        "actual_adjustments": sorted(frame["adjustment"].dropna().unique().tolist()),
        "price_fallback_rows": int(frame["price_series"].eq("day").sum()),
    }


def normalize_history(
    raw: pd.DataFrame,
    *,
    symbol: str,
    name: str,
    market: str,
    adjustment: str,
    asset_type: str = "A股",
    provider_name: str = "akshare.stock_zh_a_hist",
    source_name: str = "AKShare",
    fetched_at: str | None = None,
) -> pd.DataFrame:
    """Validate provider fields and return typed, canonical rows.

    Adjusted prices are not constrained to be positive because historical
    adjustment modes can produce negative values for older observations.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS + ["symbol", "name", "market", "asset_type", "frequency", "adjustment", "source", "provider", "provider_version", "fetched_at"])
    mapping = {key: next((column for column in names if column in raw.columns), None) for key, names in ALIASES.items()}
    missing = [key for key in ("date", "open", "high", "low", "close", "volume") if mapping[key] is None]
    if missing:
        raise StockSchemaError(f"行情数据缺少必要字段：{', '.join(missing)}。")
    data = pd.DataFrame({key: raw[column] for key, column in mapping.items() if column is not None})
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for key in ("open", "high", "low", "close", "volume", "amount"):
        if key in data:
            data[key] = pd.to_numeric(data[key].astype(str).str.replace(",", "", regex=False), errors="coerce")
    required = ["date", "open", "high", "low", "close"]
    invalid = data[required].isna().any(axis=1)
    if invalid.any():
        raise StockSchemaError(f"行情数据有 {int(invalid.sum())} 行日期或必要数值字段无法解析。")
    valid_volume = data["volume"].ge(0) & data["volume"].mod(1).eq(0)
    data["volume"] = data["volume"].where(valid_volume)
    if data["volume"].notna().all():
        data["volume"] = data["volume"].astype("int64")
    if "amount" in data:
        data["amount"] = data["amount"].where(data["amount"].ge(0) & data["amount"].lt(float("inf"))).astype("float64")
    else:
        # Tencent's adjusted K-line payload omits turnover amount; retain the
        # canonical schema and make the unavailable optional field explicit.
        data["amount"] = float("nan")
    data = data.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    data["symbol"] = symbol
    data["name"] = name
    data["market"] = market
    data["asset_type"] = asset_type
    data["frequency"] = "daily"
    data["adjustment"] = adjustment or "none"
    data["source"] = source_name
    data["provider"] = provider_name
    data["provider_version"] = "public-http-api"
    data["fetched_at"] = fetched_at or datetime.now().astimezone().isoformat()
    return data[CANONICAL_COLUMNS + ["symbol", "name", "market", "asset_type", "frequency", "adjustment", "source", "provider", "provider_version", "fetched_at"]]


def fetch_stock_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjustment: str = "qfq",
    name: str = "",
    market: str = "",
    asset_type: str = "A股",
    fetcher: Callable | None = None,
    turnover_fetcher: Callable | None = None,
) -> pd.DataFrame:
    """Fetch Tencent prices and enrich daily turnover with Sina.

    An injected price fetcher skips network enrichment unless a turnover
    fetcher is also supplied, keeping offline clients and tests offline.
    Price-fetcher volume follows Tencent's lot unit; output is shares/units.
    """
    if asset_type not in {"A股", "ETF"}:
        raise StockProviderError(f"不支持的数据类别：{asset_type}")
    if adjustment not in {"qfq", "hfq"}:
        raise StockProviderError("腾讯 K 线接口当前仅确认支持前复权和后复权；不复权日线暂不可用。")
    _tencent_market_code(symbol, market)
    provider_name = "tencent.fqkline.get"
    try:
        if fetcher is None:
            raw = fetch_tencent_history(symbol, start_date, end_date, adjustment=adjustment, market=market)
        else:
            raw = fetcher(symbol=symbol, start_date=start_date, end_date=end_date, adjustment=adjustment, market=market, asset_type=asset_type)
        raw = raw.copy()
        volume_column = next((column for column in ALIASES["volume"] if column in raw), None)
        if volume_column is not None:
            # Tencent uses rounded lots; preserve a single explicit output
            # unit even when the secondary source is unavailable.
            raw[volume_column] = (pd.to_numeric(raw[volume_column], errors="coerce") * 100).round()
        data = normalize_history(
            raw,
            symbol=symbol,
            name=name,
            market=market,
            adjustment=adjustment,
            asset_type=asset_type,
            provider_name=provider_name,
            source_name="Tencent",
        )
        data["requested_adjustment"] = adjustment
        data["price_series"] = f"{adjustment}day"
        if "price_series" in raw and not raw.empty:
            date_column = next(column for column in ALIASES["date"] if column in raw)
            series_by_date = pd.DataFrame({
                "date": pd.to_datetime(raw[date_column], errors="coerce").dt.strftime("%Y-%m-%d"),
                "price_series": raw["price_series"],
            }).drop_duplicates("date", keep="last").set_index("date")["price_series"]
            data["price_series"] = data["date"].map(series_by_date)
        data["adjustment"] = data["price_series"].map({"qfqday": "qfq", "hfqday": "hfq", "day": "none"})
        if data["adjustment"].isna().any():
            raise StockSchemaError("腾讯价格序列字段无法映射到实际复权方式。")
        data["volume_unit"] = "份" if asset_type == "ETF" else "股"
        data["amount_unit"] = "元"
        data["volume_source"] = provider_name
        data["amount_source"] = data["amount"].notna().map({True: provider_name, False: ""})
        data["turnover_status"] = data[["volume", "amount"]].notna().all(axis=1).map({True: "COMPLETE", False: "PARTIAL"})
        data["turnover_message"] = data[["volume", "amount"]].notna().all(axis=1).map({
            True: "行情来源已提供成交额。",
            False: "成交额尚未补充；腾讯成交量已从手换算为股/份。",
        })
        data["turnover_fetched_at"] = ""
        if not data.empty and (fetcher is None or turnover_fetcher is not None):
            try:
                supplement_fetcher = turnover_fetcher or fetch_sina_turnover
                supplement = supplement_fetcher(symbol, start_date, end_date, market=market)
                supplement = normalize_turnover(supplement, start_date, end_date, allow_partial=True)
                data = enrich_turnover(data, supplement)
            except Exception as exc:
                # Secondary-source failure must not discard valid prices.
                data["turnover_status"] = "ERROR"
                data["turnover_message"] = f"成交数据补充失败，保留腾讯行情：{type(exc).__name__}: {exc}"
        return data
    except (StockSchemaError, StockProviderError):
        raise
    except Exception as exc:
        raise StockProviderError(f"腾讯行情请求失败（网络、限流或接口错误）：{type(exc).__name__}: {exc}") from exc
