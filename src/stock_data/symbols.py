from __future__ import annotations

import re
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable

import pandas as pd

from src.stock_data.errors import StockUniverseError
from src.stock_data.files import atomic_write_csv, atomic_write_json, read_json


UNIVERSE_FILENAME = "universe.csv"
UNIVERSE_METADATA_FILENAME = "universe.json"
UNIVERSE_COLUMNS = ["symbol", "name", "market"]
CODE_PATTERN = re.compile(r"^\d{6}$")


def stock_market(symbol: str) -> str:
    code = str(symbol).zfill(6)
    if code.startswith(("4", "8", "92")):
        return "北京"
    if code.startswith("6"):
        return "上海"
    if code.startswith(("0", "3")):
        return "深圳"
    return "未知"


def normalize_stock_universe(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        raise StockUniverseError("AKShare 股票代码表为空。")
    aliases = {
        "symbol": ("code", "证券代码", "股票代码", "代码", "A股代码"),
        "name": ("name", "证券简称", "股票简称", "名称", "A股简称"),
    }
    resolved: dict[str, str] = {}
    for target, options in aliases.items():
        match = next((column for column in options if column in raw.columns), None)
        if match is None:
            raise StockUniverseError(f"股票代码表缺少必要字段：{target}。")
        resolved[target] = match
    work = pd.DataFrame(
        {
            "symbol": raw[resolved["symbol"]].astype("string").str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6),
            "name": raw[resolved["name"]].astype("string").str.strip(),
        }
    )
    work = work[work["symbol"].str.fullmatch(CODE_PATTERN).fillna(False)]
    work = work[work["name"].notna() & work["name"].ne("")]
    work["market"] = work["symbol"].map(stock_market)
    work = work[work["market"].ne("未知")]
    work = work.drop_duplicates("symbol", keep="last").sort_values("symbol")
    if work.empty:
        raise StockUniverseError("股票代码表清洗后没有有效沪深京 A 股记录。")
    return work[UNIVERSE_COLUMNS].reset_index(drop=True)


def _akshare_version() -> str:
    try:
        return version("akshare")
    except PackageNotFoundError:
        return "unknown"


def stock_universe_paths(data_dir: str | Path = "data/stocks") -> tuple[Path, Path]:
    base = Path(data_dir)
    return base / UNIVERSE_FILENAME, base / UNIVERSE_METADATA_FILENAME


def read_stock_universe_cache(data_dir: str | Path = "data/stocks") -> tuple[pd.DataFrame, dict]:
    csv_path, metadata_path = stock_universe_paths(data_dir)
    if not csv_path.exists():
        return pd.DataFrame(columns=UNIVERSE_COLUMNS), {}
    try:
        frame = pd.read_csv(csv_path, dtype={"symbol": str})
    except Exception as exc:
        raise StockUniverseError(f"本地股票代码表无法读取：{exc}") from exc
    if not set(UNIVERSE_COLUMNS).issubset(frame.columns):
        raise StockUniverseError("本地股票代码表字段不完整；请显式更新代码表。")
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame[UNIVERSE_COLUMNS].drop_duplicates("symbol").reset_index(drop=True), read_json(metadata_path)


def get_stock_universe(
    *,
    data_dir: str | Path = "data/stocks",
    allow_fetch_if_missing: bool = False,
    force_refresh: bool = False,
    fetcher: Callable[[], pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, dict]:
    csv_path, metadata_path = stock_universe_paths(data_dir)
    if csv_path.exists() and not force_refresh:
        return read_stock_universe_cache(data_dir)
    if not csv_path.exists() and not allow_fetch_if_missing and not force_refresh:
        raise StockUniverseError("本地股票代码表尚未建立；请点击搜索或更新股票代码表后再试。")
    try:
        if fetcher is None:
            import akshare as ak

            fetcher = ak.stock_info_a_code_name
        normalized = normalize_stock_universe(fetcher())
        metadata = {
            "source": "AKShare",
            "api_name": "stock_info_a_code_name",
            "provider_version": _akshare_version(),
            "updated_at": datetime.now().astimezone().isoformat(),
            "record_count": int(len(normalized)),
        }
        atomic_write_csv(normalized, csv_path)
        atomic_write_json(metadata, metadata_path)
        return normalized, metadata
    except Exception as exc:
        if isinstance(exc, StockUniverseError):
            message = str(exc)
        else:
            message = f"AKShare 股票代码表获取失败：{type(exc).__name__}: {exc}"
        if csv_path.exists():
            raise StockUniverseError(f"{message}；已保留原有本地代码表。") from exc
        raise StockUniverseError(message) from exc


def search_stocks(query: str, universe: pd.DataFrame, limit: int = 100) -> pd.DataFrame:
    text = str(query or "").strip()
    if not text or universe is None or universe.empty:
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)
    if text.isdigit():
        if not CODE_PATTERN.fullmatch(text):
            return pd.DataFrame(columns=UNIVERSE_COLUMNS)
        results = universe[universe["symbol"].astype(str).eq(text)]
    else:
        results = universe[universe["name"].astype(str).str.contains(text, case=False, regex=False, na=False)]
    return results.head(max(1, int(limit)))[UNIVERSE_COLUMNS].reset_index(drop=True)


def normalize_etf_universe(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize an ETF listing returned by Sina or THS."""
    if raw is None or raw.empty:
        raise StockUniverseError("AKShare ETF 代码表为空。")
    aliases = {
        "symbol": ("symbol", "代码", "基金代码", "证券代码"),
        "name": ("name", "名称", "基金简称", "基金名称", "证券简称"),
    }
    resolved = {key: next((column for column in options if column in raw.columns), None) for key, options in aliases.items()}
    missing = [key for key, column in resolved.items() if column is None]
    if missing:
        raise StockUniverseError(f"ETF 代码表缺少必要字段：{', '.join(missing)}。")
    work = pd.DataFrame({
        "symbol": raw[resolved["symbol"]].astype("string").str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6),
        "name": raw[resolved["name"]].astype("string").str.strip(),
    })
    is_code = work["symbol"].str.fullmatch(CODE_PATTERN).fillna(False)
    is_etf_code = work["symbol"].str.startswith(("15", "50", "51", "52", "56", "58"))
    work = work[is_code & is_etf_code & work["name"].notna() & work["name"].ne("")]
    work["market"] = work["symbol"].map(lambda code: "上海" if str(code).startswith(("50", "51", "52", "56", "58")) else "深圳")
    work = work.drop_duplicates("symbol", keep="last").sort_values("symbol")
    if work.empty:
        raise StockUniverseError("ETF 代码表清洗后没有有效 ETF 记录。")
    return work[UNIVERSE_COLUMNS].reset_index(drop=True)


def fetch_etf_universe_from_sources(ak_module=None) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Merge available ETF catalogs so a single incomplete source is less likely to omit a listing."""
    if ak_module is None:
        import akshare as ak_module
    sources = (
        ("fund_etf_category_sina", lambda: ak_module.fund_etf_category_sina(symbol="ETF基金")),
        ("fund_etf_category_ths", lambda: ak_module.fund_etf_category_ths(symbol="ETF", date="")),
    )
    frames: list[pd.DataFrame] = []
    successful: list[str] = []
    errors: list[str] = []
    for name, fetch in sources:
        try:
            normalized = normalize_etf_universe(fetch())
            frames.append(normalized)
            successful.append(name)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if not frames:
        raise StockUniverseError("所有 ETF 代码表来源均不可用：" + "；".join(errors))
    merged = pd.concat(frames, ignore_index=True).drop_duplicates("symbol", keep="last").sort_values("symbol")
    return merged[UNIVERSE_COLUMNS].reset_index(drop=True), successful, errors


def get_etf_universe(
    *,
    data_dir: str | Path = "data/stocks",
    allow_fetch_if_missing: bool = False,
    force_refresh: bool = False,
    fetcher: Callable[[], pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, dict]:
    csv_path = Path(data_dir) / "etf_universe.csv"
    metadata_path = Path(data_dir) / "etf_universe.json"
    if csv_path.exists() and not force_refresh:
        try:
            frame = pd.read_csv(csv_path, dtype={"symbol": str})
            if not set(UNIVERSE_COLUMNS).issubset(frame.columns):
                raise StockUniverseError("本地 ETF 代码表字段不完整；请更新 ETF 代码表。")
            frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
            return frame[UNIVERSE_COLUMNS].drop_duplicates("symbol").reset_index(drop=True), read_json(metadata_path)
        except Exception as exc:
            raise StockUniverseError(f"本地 ETF 代码表无法读取：{exc}") from exc
    if not csv_path.exists() and not allow_fetch_if_missing and not force_refresh:
        raise StockUniverseError("本地 ETF 代码表尚未建立；搜索 ETF 时会尝试获取并缓存。")
    try:
        if fetcher is None:
            normalized, sources, source_errors = fetch_etf_universe_from_sources()
        else:
            normalized = normalize_etf_universe(fetcher())
            sources, source_errors = ["injected_fetcher"], []
        metadata = {
            "source": "AKShare",
            "api_name": "+".join(sources),
            "source_errors": source_errors,
            "provider_version": _akshare_version(),
            "updated_at": datetime.now().astimezone().isoformat(),
            "record_count": int(len(normalized)),
        }
        atomic_write_csv(normalized, csv_path)
        atomic_write_json(metadata, metadata_path)
        return normalized, metadata
    except Exception as exc:
        message = str(exc) if isinstance(exc, StockUniverseError) else f"AKShare ETF 代码表获取失败：{type(exc).__name__}: {exc}"
        if csv_path.exists():
            raise StockUniverseError(f"{message}；已保留原有本地 ETF 代码表。") from exc
        raise StockUniverseError(message) from exc
