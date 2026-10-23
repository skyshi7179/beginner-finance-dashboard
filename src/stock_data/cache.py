"""Independent, atomic CSV cache for stock OHLCV data."""

from __future__ import annotations

from datetime import date, timedelta
import os
import tempfile
from pathlib import Path

import pandas as pd

from src.stock_data.errors import StockCacheError
from src.stock_data.files import atomic_write_csv, atomic_write_json, read_json
from src.stock_data.provider import CANONICAL_COLUMNS, PRICE_COLUMNS
from src.stock_data.turnover import TURNOVER_COLUMNS


HISTORY_COLUMNS = CANONICAL_COLUMNS + ["symbol", "name", "market", "asset_type", "frequency", "adjustment", "source", "provider", "provider_version", "fetched_at"] + PRICE_COLUMNS + TURNOVER_COLUMNS


def cache_paths(symbol: str, frequency: str = "daily", adjustment: str = "qfq", data_dir: str | Path = "data/stocks", asset_type: str = "A股") -> tuple[Path, Path]:
    key_adjustment = adjustment or "none"
    base = Path(data_dir) / "history"
    if asset_type == "ETF":
        base = base / "etf"
    base = base / frequency / key_adjustment
    return base / f"{symbol}.csv", base / f"{symbol}.json"


def load_history_cache(symbol: str, frequency: str = "daily", adjustment: str = "qfq", data_dir: str | Path = "data/stocks", asset_type: str = "A股") -> tuple[pd.DataFrame, dict]:
    csv_path, metadata_path = cache_paths(symbol, frequency, adjustment, data_dir, asset_type)
    if not csv_path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS), {}
    try:
        frame = pd.read_csv(csv_path, dtype={"symbol": str})
        legacy_columns = set(HISTORY_COLUMNS) - {"asset_type", *TURNOVER_COLUMNS, *PRICE_COLUMNS}
        if not legacy_columns.issubset(frame.columns):
            raise StockCacheError("本地行情缓存字段不完整。")
        if "asset_type" not in frame.columns:
            frame["asset_type"] = "A股"
        if "requested_adjustment" not in frame:
            frame["requested_adjustment"] = adjustment or "none"
        if "price_series" not in frame:
            frame["price_series"] = "legacy.unspecified"
        # Old caches did not declare turnover units. Do not guess or silently
        # convert them; users can explicitly refresh to acquire verified rows.
        legacy_defaults = {
            "volume_unit": "UNKNOWN", "amount_unit": "UNKNOWN",
            "volume_source": "legacy.unspecified", "amount_source": "legacy.unspecified",
            "turnover_status": "LEGACY", "turnover_message": "旧缓存未声明成交数据单位和来源，请强制刷新所选区间。",
            "turnover_fetched_at": "",
        }
        for column, default in legacy_defaults.items():
            if column not in frame:
                frame[column] = default
            else:
                frame[column] = frame[column].fillna("")
        frame = frame[HISTORY_COLUMNS]
        for column in HISTORY_COLUMNS:
            if column not in CANONICAL_COLUMNS:
                frame[column] = frame[column].fillna("")
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce" if column in {"volume","amount"} else "raise")
        return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True), read_json(metadata_path)
    except Exception as exc:
        if isinstance(exc, StockCacheError):
            raise
        raise StockCacheError(f"本地行情缓存无法读取：{exc}") from exc


def normalize_coverage(ranges: list[dict] | None) -> list[dict]:
    intervals = sorted((str(item["start"]), str(item["end"])) for item in (ranges or []))
    merged: list[list[str]] = []
    for start, end in intervals:
        if not merged or date.fromisoformat(start) > date.fromisoformat(merged[-1][1]) + timedelta(days=1):
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [{"start": start, "end": end} for start, end in merged]


def missing_date_ranges(start: str, end: str, ranges: list[dict] | None) -> list[tuple[str, str]]:
    """Return uncovered calendar intervals; weekends are covered by successful queries too."""
    cursor = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    missing: list[tuple[str, str]] = []
    for item in normalize_coverage(ranges):
        left, right = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if right < cursor or left > end_date:
            continue
        if left > cursor:
            missing.append((cursor.isoformat(), min(end_date, left - timedelta(days=1)).isoformat()))
        cursor = max(cursor, right + timedelta(days=1))
        if cursor > end_date:
            break
    if cursor <= end_date:
        missing.append((cursor.isoformat(), end_date.isoformat()))
    return missing


def save_history_cache(frame: pd.DataFrame, metadata: dict, symbol: str, frequency: str = "daily", adjustment: str = "qfq", data_dir: str | Path = "data/stocks", asset_type: str = "A股", *, preserve_updated_at: bool = False) -> dict:
    csv_path, metadata_path = cache_paths(symbol, frequency, adjustment, data_dir, asset_type)
    old_csv = csv_path.read_bytes() if csv_path.exists() else None
    old_metadata = metadata_path.read_bytes() if metadata_path.exists() else None

    def restore(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    try:
        atomic_write_csv(frame[HISTORY_COLUMNS], csv_path)
        updated_at = (metadata.get("cache_updated_at") or metadata.get("updated_at")) if preserve_updated_at else None
        updated = {**metadata, "record_count": int(len(frame)), "cache_updated_at": updated_at or pd.Timestamp.now(tz="UTC").isoformat()}
        atomic_write_json(updated, metadata_path)
        return updated
    except Exception as exc:
        # The CSV and its coverage metadata form one logical cache entry. Roll
        # back the first replacement if committing the second file fails.
        try:
            restore(csv_path, old_csv)
            restore(metadata_path, old_metadata)
        except Exception as rollback_exc:
            raise StockCacheError(f"行情缓存提交失败，且回滚也失败：{rollback_exc}") from exc
        raise StockCacheError(f"行情缓存写入失败，保留上次完整缓存：{type(exc).__name__}: {exc}") from exc
