"""Validated orchestration for cache-first stock-history requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

import pandas as pd

from src.stock_data.cache import HISTORY_COLUMNS, load_history_cache, missing_date_ranges, normalize_coverage, save_history_cache
from src.stock_data.errors import StockInputError, StockProviderError, StockSchemaError
from src.stock_data.provider import fetch_stock_history, price_metadata
from src.stock_data.turnover import turnover_metadata, turnover_issue_summary
from src.stock_data.files import read_json
from src.time_utils import get_china_now
from src.stock_data.calendar import is_declared_trade_day


ADJUSTMENTS = {"qfq", "hfq", ""}


@dataclass
class StockHistoryResult:
    data: pd.DataFrame
    status: str
    metadata: dict
    message: str = ""


def list_cached_stock_history(data_dir: str | Path = "data/stocks") -> tuple[list[dict], list[str]]:
    """List local entries without fetching providers or rewriting cache files."""
    from src.stock_data.cache import cache_paths

    entries: list[dict] = []
    errors: list[str] = []
    for asset_type in ("A股", "ETF"):
        for adjustment in ("qfq", "hfq", ""):
            directory = cache_paths("000000", "daily", adjustment, data_dir, asset_type)[0].parent
            for path in sorted(directory.glob("*.csv")):
                if not re.fullmatch(r"\d{6}", path.stem):
                    continue
                try:
                    metadata = read_json(path.with_suffix(".json"))
                    head = pd.read_csv(path, nrows=1, dtype={"symbol": str})
                    first = head.iloc[0] if not head.empty else pd.Series(dtype="object")
                    ranges = metadata.get("coverage_ranges", [])
                    dates = pd.read_csv(path, usecols=["date"])["date"].dropna()
                    entries.append({"symbol": path.stem,
                                    "name": str(first.get("name", "")) if pd.notna(first.get("name")) else "",
                                    "market": str(first.get("market", "")) if pd.notna(first.get("market")) else "",
                                    "asset_type": asset_type, "adjustment": adjustment,
                                    "start": str(dates.min()) if not dates.empty else None,
                                    "end": str(dates.max()) if not dates.empty else None,
                                    "updated_at": metadata.get("cache_updated_at") or metadata.get("updated_at", ""),
                                    "record_count": metadata.get("record_count", 0)})
                except Exception as exc:
                    errors.append(f"{asset_type} {path.stem} {adjustment or 'none'}：{exc}")
    return entries, errors


def validate_request(symbol: str, start_date: date | str, end_date: date | str, adjustment: str = "qfq") -> tuple[str, str, str]:
    code = str(symbol).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise StockInputError("股票代码必须是 6 位数字。")
    try:
        start = pd.Timestamp(start_date).date().isoformat()
        end = pd.Timestamp(end_date).date().isoformat()
    except Exception as exc:
        raise StockInputError("开始和结束日期必须是有效日期。") from exc
    if start > end:
        raise StockInputError("开始日期不能晚于结束日期。")
    if adjustment not in ADJUSTMENTS:
        raise StockInputError("复权方式仅支持前复权、后复权或不复权。")
    return code, start, end


def _slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame[frame["date"].between(start, end)].reset_index(drop=True)


def _data_message(message: str, metadata: dict) -> str:
    fallback = metadata.get("price_fallback_rows", 0)
    if fallback:
        message += f" 腾讯返回 {fallback} 条原始日线（day），未提供所选复权序列；已记录实际复权方式为不复权。"
    issue = turnover_issue_summary(metadata)
    if issue:
        return f"{message} {issue} 可查看逐日来源与校验说明；必要时强制刷新重试。"
    return message


def _data_metadata(frame: pd.DataFrame) -> dict:
    return {**price_metadata(frame), **turnover_metadata(frame)}


def unfinished_today(frame: pd.DataFrame) -> bool:
    """A pre-close candle in an old cache must not masquerade as finalized."""
    if frame.empty or "fetched_at" not in frame:
        return False
    now = get_china_now()
    today = now.date().isoformat()
    rows = frame[frame["date"].eq(today)]
    if rows.empty:
        return False
    captured = pd.to_datetime(rows["fetched_at"], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")
    return bool(captured.isna().any() or (captured.dt.date != now.date()).any() or (captured.dt.time < time(15)).any())


def read_stock_history_cache(symbol: str, start_date: date | str, end_date: date | str, *, adjustment: str = "qfq", data_dir: str | Path = "data/stocks", asset_type: str = "A股") -> StockHistoryResult:
    if asset_type not in {"A股", "ETF"}:
        raise StockInputError("证券类别仅支持 A股 或 ETF。")
    code, start, end = validate_request(symbol, start_date, end_date, adjustment)
    frame, metadata = load_history_cache(code, "daily", adjustment, data_dir, asset_type)
    missing = missing_date_ranges(start, end, metadata.get("coverage_ranges", []))
    selected = _slice(frame, start, end)
    pending_final = unfinished_today(selected)
    metadata = {**metadata, **_data_metadata(selected)}
    message = "本地缓存已覆盖请求区间。" if not missing else "本地缓存未覆盖完整请求区间。"
    if pending_final:
        message += " 今日缓存采集于收盘前或时间无法确认，尚未确认完整日线；收盘后请主动更新。"
    return StockHistoryResult(selected, "CACHE" if not missing and not pending_final else "STALE_CACHE" if not frame.empty else "CACHE_MISS", metadata, _data_message(message, metadata))


def get_stock_history(symbol: str, start_date: date | str, end_date: date | str, *, adjustment: str = "qfq", name: str = "", market: str = "", asset_type: str = "A股", force_refresh: bool = False, data_dir: str | Path = "data/stocks", fetcher: Callable | None = None, turnover_fetcher: Callable | None = None) -> StockHistoryResult:
    if asset_type not in {"A股", "ETF"}:
        raise StockInputError("证券类别仅支持 A股 或 ETF。")
    code, start, end = validate_request(symbol, start_date, end_date, adjustment)
    try:
        cached, metadata = load_history_cache(code, "daily", adjustment, data_dir, asset_type)
    except Exception as exc:
        return StockHistoryResult(pd.DataFrame(columns=HISTORY_COLUMNS), "ERROR", {}, f"本地行情缓存无法读取：{exc}")
    missing = [(start, end)] if force_refresh else missing_date_ranges(start, end, metadata.get("coverage_ranges", []))
    now = get_china_now()
    today = now.date().isoformat()
    latest_missing = is_declared_trade_day(today) and (cached.empty or not cached["date"].eq(today).any())
    if not force_refresh and now.time() >= time(15) and start <= today <= end and (unfinished_today(cached) or latest_missing):
        missing = [(item["start"], item["end"]) for item in normalize_coverage(
            [{"start":left,"end":right} for left,right in missing] + [{"start":today,"end":today}])]
    if not missing:
        selected = _slice(cached, start, end)
        metadata = {**metadata, **_data_metadata(selected)}
        return StockHistoryResult(selected, "CACHE", metadata, _data_message("本地缓存已覆盖请求区间。", metadata))
    parts: list[pd.DataFrame] = []
    try:
        for left, right in missing:
            parts.append(fetch_stock_history(code, left, right, adjustment=adjustment, name=name, market=market, asset_type=asset_type, fetcher=fetcher, turnover_fetcher=turnover_fetcher))
    except Exception as exc:
        status = "STALE_CACHE" if not cached.empty else "ERROR"
        return StockHistoryResult(_slice(cached, start, end), status, metadata, f"新数据获取失败，原缓存未改动：{exc}")
    fetched = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=HISTORY_COLUMNS)
    if not fetched.empty:
        fetched = fetched[fetched["date"].between(start, end)].reset_index(drop=True)
        if not cached.empty:
            previous = cached.set_index("date")
            for index, row in fetched.iterrows():
                if row["turnover_status"] == "COMPLETE" or row["date"] not in previous.index:
                    continue
                old = previous.loc[row["date"]]
                if old["turnover_status"] not in {"COMPLETE", "STALE"}:
                    continue
                # Keep last verified turnover when its refresh fails, but
                # retain the old timestamp and explicitly mark it stale.
                for column in ("volume", "amount", "volume_unit", "amount_unit", "volume_source", "amount_source", "turnover_fetched_at"):
                    fetched.at[index, column] = old[column]
                fetched.at[index, "turnover_status"] = "STALE"
                fetched.at[index, "turnover_message"] = f"本次成交数据未通过核验，保留上次成交数据：{row['turnover_message']}"
    merged = pd.concat([cached, fetched], ignore_index=True)
    if not merged.empty:
        merged = merged.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    new_coverage = normalize_coverage([*(metadata.get("coverage_ranges", [])), *[{"start": left, "end": right} for left, right in missing]])
    awaiting_today = start <= today <= end and now.time() >= time(15) and is_declared_trade_day(today) and (merged.empty or not merged["date"].eq(today).any())
    if awaiting_today:
        # A successful but not-yet-updated upstream response must not cache an
        # empty final trading day forever. Holidays still retain query coverage.
        before = (pd.Timestamp(today) - pd.Timedelta(days=1)).date().isoformat()
        after = (pd.Timestamp(today) + pd.Timedelta(days=1)).date().isoformat()
        retained = []
        for interval in new_coverage:
            left, right = interval["start"], interval["end"]
            if not left <= today <= right:
                retained.append(interval)
            else:
                if left < today: retained.append({"start":left,"end":before})
                if today < right: retained.append({"start":after,"end":right})
        new_coverage = normalize_coverage(retained)
    provider_name = "tencent.fqkline.get"
    updates = {**metadata, "symbol": code, "asset_type": asset_type, "frequency": "daily", "adjustment": adjustment or "none", "source": "Tencent", "provider": provider_name, "provider_version": "public-http-api", "updated_at": datetime.now().astimezone().isoformat(), "coverage_ranges": new_coverage,
               "history_completeness":"UNVERIFIED", "earliest_observed_date":str(merged["date"].min()) if not merged.empty else None,
               "latest_observed_date":str(merged["date"].max()) if not merged.empty else None, **_data_metadata(merged)}
    try:
        saved = save_history_cache(merged, updates, code, "daily", adjustment, data_dir, asset_type)
    except Exception as exc:
        status = "STALE_CACHE" if not cached.empty else "ERROR"
        return StockHistoryResult(_slice(cached, start, end), status, metadata, str(exc))
    selected = _slice(merged, start, end)
    saved = {**saved, **_data_metadata(selected)}
    message = "已获取并保存行情数据。" + (" 今日完整日线尚未返回，未将今天记录为覆盖；请稍后主动重试。" if awaiting_today else "")
    return StockHistoryResult(selected, "LIVE_FETCHED", saved, _data_message(message, saved))
