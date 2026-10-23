"""Streamlit page for explicit A-share search and local-first history retrieval."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from src.stock_data.errors import StockDataError
from src.stock_data.workspace import get_stock_history, read_stock_history_cache, storage_choice
from src.date_controls import date_controls
from src.stock_data.symbols import get_etf_universe, get_stock_universe, search_stocks
from src.stock_data.context import restore_acquisition_context, save_research_context
from src.stock_data.turnover import turnover_issue_summary
from src.stock_data.units import turnover_gaps
from src.navigation import PAGE_VISUALIZATION, navigate_to


FIELD_DISPLAY_NAMES = {
    "date": "date（日期）",
    "open": "open（开盘价）",
    "high": "high（最高价）",
    "low": "low（最低价）",
    "close": "close（收盘价）",
    "volume": "volume（成交量）",
    "amount": "amount（成交额）",
    "adjustment": "adjustment（实际复权）",
}

TURNOVER_DISPLAY_NAMES = {
    "date": "日期", "volume_source": "成交量来源", "amount_source": "成交额来源",
    "volume_unit": "成交量单位", "amount_unit": "成交额单位", "turnover_status": "成交数据状态",
    "turnover_message": "校验说明", "turnover_fetched_at": "成交数据采集时间",
}

DATA_DIR = Path("data/stocks")


def _stock_label(row: pd.Series) -> str:
    return f"{row['symbol']}  {row['name']}  ·  {row['market']}"


def render_stock_data_page() -> bool:
    """Render stock search and history page. Return True when home is requested."""
    if st.button("← 返回首页", key="stock_data_return_home"):
        return True
    st.title("股票 / ETF 数据获取")
    st.caption("点击获取后查询行情：腾讯提供复权价格，新浪补充成交量和成交额；优先使用本地缓存。")
    restore_acquisition_context(st.session_state)

    if "stock_search_results" not in st.session_state:
        st.session_state.stock_search_results = []
    asset_type = st.selectbox("搜索类别", ["A股", "ETF"], key="stock_search_asset_type")
    refresh_label = f"刷新{asset_type}代码表"
    if st.button(refresh_label, key="stock_refresh_universe"):
        get_universe = get_etf_universe if asset_type == "ETF" else get_stock_universe
        try:
            with st.spinner(f"正在更新{asset_type}代码表…"):
                refreshed, refresh_metadata = get_universe(allow_fetch_if_missing=True, force_refresh=True)
            st.session_state.stock_search_results = []
            st.session_state.stock_search_result_type = asset_type
            st.success(f"{asset_type}代码表已更新，共 {len(refreshed)} 条。来源：{refresh_metadata.get('api_name', '--')}")
            if refresh_metadata.get("source_errors"):
                st.warning("部分来源暂不可用，清单已合并可用来源：" + "；".join(refresh_metadata["source_errors"]))
        except StockDataError as exc:
            st.error(str(exc))
    with st.form("stock_search_form"):
        query = st.text_input("代码或中文名称", placeholder="例如 600519 / 贵州茅台，或 159516 / ETF名称")
        search_clicked = st.form_submit_button("搜索", type="primary")
    search_failed = False
    if search_clicked:
        try:
            get_universe = get_etf_universe if asset_type == "ETF" else get_stock_universe
            universe, _ = get_universe(allow_fetch_if_missing=True)
            matches = search_stocks(query, universe)
            st.session_state.stock_search_results = matches.to_dict("records")
            st.session_state.stock_search_query = query
            st.session_state.stock_search_result_type = asset_type
        except StockDataError as exc:
            st.session_state.stock_search_results = []
            st.session_state.stock_search_result_type = asset_type
            search_failed = True
            message = str(exc)
            if "ProxyError" in message or "Unable to connect to proxy" in message:
                message += "\n\n这表示数据源连接所用的代理不可用；请检查系统/终端代理设置，或在可直连数据源的网络下重试。"
            st.error(message)
    results_match_category = st.session_state.get("stock_search_result_type") == asset_type
    if st.session_state.stock_search_results and results_match_category:
        options = st.session_state.stock_search_results
        labels = {_stock_label(pd.Series(item)): item for item in options}
        selected_label = st.selectbox("选择目标证券", list(labels), key=f"stock_selected_label_{asset_type}")
        selected = labels[selected_label]
        st.session_state.stock_selected = selected
        st.session_state.stock_selected_type = asset_type
    else:
        selected = st.session_state.get("stock_selected") if st.session_state.get("stock_selected_type") == asset_type else None
        if selected:
            st.info(f"当前股票：{selected['symbol']} {selected['name']} · {selected['market']}。如需更换，请先搜索并明确选择。")
        else:
            st.info(f"先搜索并从匹配结果中选择一只{asset_type}。名称搜索不会自动选中模糊匹配结果。")
    if search_clicked and not search_failed and not st.session_state.stock_search_results and query.strip():
        if asset_type == "A股" and query.strip().isdigit() and query.strip().startswith(("15", "50", "51", "52", "56", "58")):
            st.warning("该代码看起来属于场内 ETF。请将“搜索类别”切换为 ETF，再搜索；若清单较旧，可先点击“刷新ETF代码表”。")
        else:
            st.warning(f"当前{asset_type}代码表中没有匹配项。可先点击“刷新{asset_type}代码表”后重试。")

    today = date.today()
    default_start = today - timedelta(days=365)
    date_range = date_controls("stock_history_dates", default_start, today, acquisition=True, selection_key=f"{asset_type}:{selected['symbol']}" if selected else asset_type)
    adjustment_label = st.selectbox("复权方式", ["前复权（qfq）", "后复权（hfq）"], index=0, key="stock_history_adjustment")
    adjustment = {"前复权（qfq）": "qfq", "后复权（hfq）": "hfq"}[adjustment_label]
    st.caption("可请求前复权或后复权；部分证券只返回原始日线，页面将明确标注实际复权状态。北交所复权行情暂不受此接口支持。")
    storage_mode = storage_choice()
    left, right = st.columns(2)
    fetch_clicked = left.button("获取数据", type="primary", disabled=not selected, key="stock_fetch")
    force_clicked = right.button("强制刷新数据", disabled=not selected, key="stock_force_refresh", help="重新请求所选区间并覆盖同日期旧记录。")
    if not isinstance(date_range, (tuple, list)) or len(date_range) != 2:
        st.caption("请选择开始和结束日期。")
        return False
    start, end = date_range
    if selected and start <= end:
        save_research_context(st.session_state, selected, asset_type, start, end, adjustment)
    if st.button("前往可视化分析 →", key="stock_open_visualization", disabled=not selected or start > end):
        navigate_to(st.session_state, PAGE_VISUALIZATION)
        st.rerun()
    if selected and start > end:
        st.error("开始日期不能晚于结束日期。")
    elif selected and (fetch_clicked or force_clicked):
        with st.spinner("正在检查本地缓存并获取行情…"):
            result = get_stock_history(selected["symbol"], start, end, adjustment=adjustment, name=selected["name"], market=selected["market"], asset_type=asset_type, force_refresh=force_clicked, storage_mode=storage_mode, data_dir=DATA_DIR)
        st.session_state.stock_history_result = result
        st.session_state.stock_history_signature = (asset_type, selected["symbol"], start.isoformat(), end.isoformat(), adjustment)

    signature = (asset_type, selected["symbol"], start.isoformat(), end.isoformat(), adjustment) if selected and start <= end else None
    result = st.session_state.get("stock_history_result")
    if result is None or signature != st.session_state.get("stock_history_signature"):
        if selected and start <= end:
            try:
                result = read_stock_history_cache(selected["symbol"], start, end, adjustment=adjustment, asset_type=asset_type, data_dir=DATA_DIR)
                if result.status != "CACHE_MISS":
                    st.session_state.stock_history_result = result
                    st.session_state.stock_history_signature = signature
            except StockDataError as exc:
                st.error(f"本地缓存读取失败：{exc}")
    if result is not None and signature == st.session_state.get("stock_history_signature"):
        status = result.status
        (st.success if status in {"CACHE", "LIVE_FETCHED"} else st.warning if status == "STALE_CACHE" else st.error)(f"状态：{status} · {result.message}")
        if st.session_state.get("stock_history_dates_quick", "").startswith("全部历史"):
            st.info("已请求数据源可获得的历史范围；上市日期及完整上市覆盖尚未独立核验，请以实际覆盖日期为准。")
        metadata = result.metadata
        st.caption("保存方式：" + metadata.get("storage_scope", "永久") + "；可在首页→数据管理中查看或清理。")
        data = result.data
        if metadata.get("price_fallback_rows", 0):
            st.warning("腾讯仅返回原始日线 day，未返回所选复权序列。表格和 CSV 已将实际复权方式标为 none（不复权），并保留请求方式；不能据此认定接口已提供前/后复权价格。")
        dates = pd.to_datetime(data["date"]) if not data.empty else pd.Series(dtype="datetime64[ns]")
        a, b, c = st.columns(3)
        a.metric("覆盖日期", f"{dates.min().date()} 至 {dates.max().date()}" if not dates.empty else "无交易记录")
        b.metric("记录数", str(len(data)))
        c.metric("缓存更新时间", str(metadata.get("cache_updated_at") or metadata.get("updated_at") or "--"))
        if not data.empty and "fetched_at" in data:
            st.caption("价格采集时刻（不是本次读取时刻）：" + str(data["fetched_at"].max()))
        price_sources = " / ".join(sorted(data["source"].dropna().unique())) if not data.empty else metadata.get("source", "--")
        st.caption(f"证券类别：{asset_type} · 价格来源：{price_sources} · 频率：日线 · 请求复权：{adjustment or '不复权'}")
        if not data.empty:
            if dates.max().date() < end:
                st.caption(f"请求截至{end}，当前实际最后日线为{dates.max().date()}。可能是非交易日、证券尚无记录或上游尚未更新；不将请求结束日期冒充实际行情日期。")
            issue_summary = turnover_issue_summary(metadata)
            if issue_summary:
                st.warning(f"成交数据状态：{metadata.get('turnover_status', 'PARTIAL')}。{issue_summary} 旧缓存可点击“强制刷新数据”重试补充。")
            units = " / ".join(metadata.get("volume_units", [])) or "未声明"
            amount_units = " / ".join(metadata.get("amount_units", [])) or "未声明"
            st.caption(f"成交量单位：{units}；成交额单位：{amount_units}。成交额为实际交易金额，不随价格复权调整。")
            preview = [column for column in FIELD_DISPLAY_NAMES if column in data]
            display_names = FIELD_DISPLAY_NAMES.copy()
            for field, label in (("volume", "成交量"), ("amount", "成交额")):
                unit_column = f"{field}_unit"
                declared_units = data[unit_column].fillna("UNKNOWN").unique().tolist() if unit_column in data else []
                if len(declared_units) == 1 and declared_units[0] not in {"UNKNOWN", ""}:
                    unit_label = declared_units[0]
                else:
                    unit_label = "单位不一致，见明细" if len(declared_units) > 1 else "单位未确认"
                display_names[field] = f"{field}（{label}，{unit_label}）"
            display_data = data[preview].rename(columns=display_names)
            st.dataframe(display_data, use_container_width=True, hide_index=True)
            with st.expander("成交数据来源与校验"):
                issues = turnover_gaps(data)
                if not issues.empty:
                    st.caption("下表每一行都有价格行情，只说明成交量/成交额字段的核验状态；休市日不生成行情行。")
                    st.dataframe(issues, use_container_width=True, hide_index=True)
                audit_columns = [column for column in TURNOVER_DISPLAY_NAMES if column in data]
                st.dataframe(data[audit_columns].rename(columns=TURNOVER_DISPLAY_NAMES), use_container_width=True, hide_index=True)
            st.download_button("下载 CSV", data=data.to_csv(index=False).encode("utf-8-sig"), file_name=f"{selected['symbol']}_{start}_{end}_{adjustment or 'none'}.csv", mime="text/csv", key="stock_history_download", help="CSV 保留英文标准字段、单位、来源及校验状态，便于复用和追溯。")
        else:
            st.info("所选日期区间没有交易日行情记录；请求区间已按接口成功结果记录，可直接查看其他日期。")
    return False
