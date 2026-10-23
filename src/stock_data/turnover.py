"""Sina daily turnover adapter and explicit, date-aligned field enrichment.

Sina returns the full unadjusted history; we slice it locally rather than claim
incremental downloading. Volume is shares (stocks) / units (ETFs), amount CNY.
Tencent's daily volume is rounded in lots. Cross-source merges allow at most
one lot (100 shares/units) of rounding difference and retain the price source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import numpy as np
import pandas as pd
import requests

from src.stock_data.errors import StockProviderError, StockSchemaError


TURNOVER_COLUMNS = [
    "volume_unit", "amount_unit", "volume_source", "amount_source",
    "turnover_status", "turnover_message", "turnover_fetched_at",
]
SINA_PROVIDER = "sina.klc_kl"


def decode_sina_history(text: str) -> list:
    # Reuse AKShare's bundled decoder, never evaluate downloaded JavaScript.
    from akshare.stock.cons import hk_js_decode
    import py_mini_racer

    decoder = py_mini_racer.MiniRacer()
    try:
        decoder.eval(hk_js_decode)
        encoded = text.split("=", 1)[1].split(";", 1)[0].strip().strip('"')
        return decoder.call("d", encoded)
    finally:
        decoder.close()


def normalize_turnover(raw: pd.DataFrame, start_date: str, end_date: str, *, allow_partial: bool = False) -> pd.DataFrame:
    required = ["date", "volume", "amount"]
    if raw.empty:
        return pd.DataFrame(columns=required)
    if not set(required).issubset(raw.columns):
        raise StockSchemaError("新浪历史成交数据缺少 date、volume 或 amount 字段。")
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], format="ISO8601", errors="coerce").dt.strftime("%Y-%m-%d")
    if frame["date"].isna().any():
        raise StockSchemaError("新浪历史成交数据包含无法识别的日期。")
    frame = frame[frame["date"].between(start_date, end_date)].copy()
    for column in ("volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if (not np.isfinite(frame[column]).all()) or (frame[column] < 0).any():
            if not allow_partial:
                raise StockSchemaError(f"新浪 {column} 包含缺失、非数值或负数。")
            frame.loc[~np.isfinite(frame[column]) | frame[column].lt(0), column] = np.nan
    fraction = frame["volume"].notna() & frame["volume"].mod(1).ne(0)
    if fraction.any():
        if not allow_partial:
            raise StockSchemaError("新浪成交量必须为整数股数或份数，不能静默截断。")
        frame.loc[fraction, "volume"] = np.nan
    mismatch = frame[["volume", "amount"]].notna().all(axis=1) & ((frame["volume"] == 0) != (frame["amount"] == 0))
    if mismatch.any():
        if not allow_partial:
            raise StockSchemaError("新浪成交量与成交额的零值不一致。")
        frame.loc[mismatch, "amount"] = np.nan
    # When raw high/low are available, catch obvious unit/schema drift using
    # the unadjusted daily trading range, never adjusted Tencent prices.
    if {"low", "high"}.issubset(frame.columns):
        low = pd.to_numeric(frame["low"], errors="coerce")
        high = pd.to_numeric(frame["high"], errors="coerce")
        average = frame["amount"] / frame["volume"].replace(0, np.nan)
        invalid = (frame["volume"] > 0) & frame["amount"].notna() & (
            ~np.isfinite(low) | ~np.isfinite(high) | (low > high)
            | (average < low - 0.011) | (average > high + 0.011)
        )
        if invalid.any():
            if not allow_partial:
                raise StockSchemaError("新浪成交额/成交量不在原始日内价格范围内，可能存在单位或字段变化。")
            frame.loc[invalid, "amount"] = np.nan
    conflicts = frame.groupby("date")[["volume", "amount"]].nunique().gt(1).any(axis=1)
    if conflicts.any():
        raise StockSchemaError("新浪同一日期返回了相互冲突的成交数据。")
    if frame["volume"].notna().all():
        frame["volume"] = frame["volume"].astype("int64")
    frame["amount"] = frame["amount"].astype("float64")
    return frame[required].drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch_sina_turnover(
    symbol: str, start_date: str, end_date: str, *, market: str,
    http_get: Callable = requests.get, decoder: Callable = decode_sina_history,
) -> pd.DataFrame:
    if market == "北京" or symbol.startswith(("4", "8", "92")):
        raise StockProviderError("新浪成交数据补充当前仅验证沪深证券。")
    prefix = "sh" if market == "上海" or symbol.startswith(("5", "6", "9")) else "sz"
    url = f"https://finance.sina.com.cn/realstock/company/{prefix}{symbol}/hisdata_klc2/klc_kl.js"
    try:
        response = http_get(url, timeout=20)
        response.raise_for_status()
        raw = pd.DataFrame(decoder(response.text))
        return normalize_turnover(raw, start_date, end_date, allow_partial=True)
    except StockProviderError:
        raise
    except Exception as exc:
        raise StockProviderError(f"新浪历史成交数据请求或解码失败：{type(exc).__name__}: {exc}") from exc


def enrich_turnover(history: pd.DataFrame, supplement: pd.DataFrame) -> pd.DataFrame:
    """Use a left join and validate volume before accepting a Sina row."""
    result = history.copy()
    rows = supplement.set_index("date")
    fetched_at = datetime.now().astimezone().isoformat()
    for index, row in result.iterrows():
        if row["date"] not in rows.index:
            result.at[index, "turnover_status"] = "PARTIAL"
            result.at[index, "turnover_message"] = "新浪没有对应日期，保留腾讯成交量，成交额暂缺。"
            continue
        candidate = rows.loc[row["date"]]
        if pd.isna(candidate["volume"]):
            result.at[index, "turnover_status"] = "PARTIAL"
            result.at[index, "turnover_message"] = "该日新浪成交量不可核验，保留腾讯成交量；不据此补充成交额。"
            continue
        difference = abs(float(row["volume"]) - float(candidate["volume"])) if pd.notna(row["volume"]) else 0.
        zero_conflict = (row["volume"] == 0) != (candidate["volume"] == 0)
        if difference > 100 or zero_conflict:
            result.at[index, "turnover_status"] = "CONFLICT"
            result.at[index, "turnover_message"] = f"两源成交量相差 {difference:g} 股/份，未合并新浪数据。"
            continue
        result.at[index, "volume"] = int(candidate["volume"])
        result.at[index, "amount"] = float(candidate["amount"])
        result.at[index, "volume_source"] = SINA_PROVIDER
        complete = pd.notna(candidate["amount"])
        result.at[index, "amount_source"] = SINA_PROVIDER if complete else ""
        result.at[index, "turnover_status"] = "COMPLETE" if complete else "PARTIAL"
        result.at[index, "turnover_message"] = "按交易日期对齐，成交量差在一手舍入容差内。" if complete else "成交量已核验，该日成交额缺失或无效，保留空值。"
        result.at[index, "turnover_fetched_at"] = fetched_at
    return result


def turnover_metadata(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"turnover_status": "NO_ROWS", "turnover_missing_rows": 0,
                "turnover_incomplete_rows": 0, "turnover_field_missing_rows": 0,
                "turnover_conflict_rows": 0, "turnover_status_counts": {}}
    complete = frame["turnover_status"].eq("COMPLETE")
    counts = frame["turnover_status"].fillna("UNKNOWN").value_counts().to_dict()
    field_missing = frame[["volume", "amount"]].isna().any(axis=1)
    amount_units = sorted(frame["amount_unit"].dropna().unique().tolist())
    return {
        "turnover_status": "COMPLETE" if complete.all() else "PARTIAL",
        # Retained for old cached metadata/readers.  UI must call these rows
        # incomplete/awaiting verification rather than missing trading days.
        "turnover_missing_rows": int((~complete).sum()),
        "turnover_incomplete_rows": int((~complete).sum()),
        "turnover_field_missing_rows": int(field_missing.sum()),
        "turnover_conflict_rows": int(frame["turnover_status"].eq("CONFLICT").sum()),
        "turnover_status_counts": {str(key): int(value) for key, value in counts.items()},
        "volume_sources": sorted(frame["volume_source"].dropna().unique().tolist()),
        "amount_sources": sorted(value for value in frame["amount_source"].dropna().unique() if value),
        "volume_units": sorted(frame["volume_unit"].dropna().unique().tolist()),
        "amount_units": amount_units,
        "amount_unit": amount_units[0] if len(amount_units) == 1 else "MIXED",
    }


def turnover_issue_summary(metadata: dict) -> str:
    """Explain row-level turnover verification without implying missing bars."""
    total = int(metadata.get("turnover_incomplete_rows", metadata.get("turnover_missing_rows", 0)) or 0)
    if not total:
        return ""
    labels = {
        "CONFLICT": "来源冲突", "PARTIAL": "字段待补充", "ERROR": "补充源失败",
        "STALE": "沿用旧值", "LEGACY": "旧缓存口径未确认", "UNKNOWN": "状态未知",
    }
    counts = metadata.get("turnover_status_counts") or {}
    details = [f"{labels.get(status, status)}{int(count)}条" for status, count in counts.items()
               if status != "COMPLETE" and int(count)]
    suffix = f"（{'，'.join(details)}）" if details else ""
    return (f"{total} 条已有价格日线的成交字段待核验{suffix}。"
            "这不表示缺少交易日；法定休市日本来就没有日线，也不会计入这里。")
