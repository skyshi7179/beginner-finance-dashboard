"""UI-facing cache selection. The original service remains the only downloader."""
from pathlib import Path
import pandas as pd
import streamlit as st

from src.stock_data import service
from src.stock_data.cache import cache_paths, load_history_cache, save_history_cache
from src.stock_data.session_cache import get_session_id, session_directory, protect, start_cleaner
from src.stock_data.errors import StockDataError

PERMANENT = "保存到本地"
TEMPORARY = "仅本次使用（临时保存）"


def temporary_root(data_dir="data/stocks") -> Path:
    return Path(data_dir).parent / "session_cache"


def temporary_directory(data_dir="data/stocks") -> Path:
    return session_directory(temporary_root(data_dir), get_session_id(st.session_state)) / "stocks"


def storage_choice(key="stock_storage_mode") -> str:
    if key not in st.session_state:
        st.session_state[key] = st.session_state.get("storage_preference", PERMANENT)
    mode = st.radio("行情保存方式", [PERMANENT, TEMPORARY], horizontal=True, key=key)
    st.session_state.storage_preference = mode
    if mode == TEMPORARY:
        st.caption("临时保存到项目data/session_cache；跨页面可复用。会话失去活跃信号约10分钟后清理；应用停止时由下次启动兜底。刷新/休眠过久可能丢失，可在数据管理中清理或转存。")
    else:
        st.caption("保存到data/stocks，下次仍可使用；可在“数据管理”中删除。浏览器下载文件不随缓存清理。")
    return mode


def list_cached_stock_history(data_dir="data/stocks"):
    permanent, errors = service.list_cached_stock_history(data_dir)
    temporary, extra = service.list_cached_stock_history(temporary_directory(data_dir))
    by_key = {}
    def stamp(value):
        try:
            return pd.Timestamp(value).timestamp()
        except (ValueError, TypeError):
            return 0
    for entries, scope in ((permanent, "永久"), (temporary, "本次临时")):
        for entry in entries:
            key = (entry["asset_type"], entry["symbol"], entry["adjustment"])
            candidate = {**entry, "storage_scope": scope}
            if key not in by_key or stamp(candidate["updated_at"]) >= stamp(by_key[key]["updated_at"]):
                by_key[key] = candidate
    return list(by_key.values()), errors + extra


def read_stock_history_cache(symbol, start_date, end_date, *, adjustment="qfq", data_dir="data/stocks", asset_type="A股"):
    entries, _ = list_cached_stock_history(data_dir)
    item = next((e for e in entries if e["symbol"] == symbol and e["asset_type"] == asset_type and e["adjustment"] == adjustment), {})
    scope = item.get("storage_scope", "永久")
    directory = temporary_directory(data_dir) if scope == "本次临时" else data_dir
    result = service.read_stock_history_cache(symbol, start_date, end_date, adjustment=adjustment, asset_type=asset_type, data_dir=directory)
    result.metadata["storage_scope"] = scope
    return result


def _get_stock_history(symbol, start_date, end_date, *, data_dir="data/stocks", storage_mode=PERMANENT, **kwargs):
    if storage_mode == PERMANENT:
        result = service.get_stock_history(symbol, start_date, end_date, data_dir=data_dir, **kwargs)
        result.metadata["storage_scope"] = "永久"
        return result
    sid = get_session_id(st.session_state)
    root = temporary_root(data_dir)
    st.session_state.temporary_cache_roots = sorted(set(st.session_state.get("temporary_cache_roots", []) + [str(root.resolve())]))
    start_cleaner(root)
    with protect(root, sid) as session:
        target = session / "stocks"
        adjustment, asset = kwargs.get("adjustment", "qfq"), kwargs.get("asset_type", "A股")
        # Seed an existing permanent entry without modifying it. This preserves
        # cache-first semantics and allows missing ranges to merge in isolation.
        if not cache_paths(symbol, "daily", adjustment, target, asset)[0].exists():
            frame, metadata = load_history_cache(symbol, "daily", adjustment, data_dir, asset)
            if not frame.empty:
                save_history_cache(frame, metadata, symbol, "daily", adjustment, target, asset, preserve_updated_at=True)
        result = service.get_stock_history(symbol, start_date, end_date, data_dir=target, **kwargs)
        result.metadata["storage_scope"] = "本次临时"
        return result


def get_stock_history(symbol, start_date, end_date, *, data_dir="data/stocks", storage_mode=PERMANENT, **kwargs):
    try:
        return _get_stock_history(symbol,start_date,end_date,data_dir=data_dir,storage_mode=storage_mode,**kwargs)
    except (OSError,ValueError,StockDataError) as exc:
        try:
            old=service.read_stock_history_cache(symbol,start_date,end_date,data_dir=data_dir,
                adjustment=kwargs.get("adjustment","qfq"),asset_type=kwargs.get("asset_type","A股"))
        except (OSError,ValueError,StockDataError):
            old=service.StockHistoryResult(pd.DataFrame(),"ERROR",{})
        old.status="STALE_CACHE" if not old.data.empty else "ERROR"
        old.metadata["storage_scope"]="永久缓存回退（临时操作失败）" if storage_mode==TEMPORARY else "永久"
        old.message=f"缓存操作未完成，原永久缓存未改动：{exc}"
        return old
