"""Streamlit research page: local cache reads only, no provider calls."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from src.navigation import PAGE_HOME, PAGE_DATA_ACQUISITION, PAGE_BACKTEST
from src.stock_data.context import ADJUSTMENT_LABELS, save_research_context
from src.stock_data.errors import StockDataError
from src.stock_data.workspace import list_cached_stock_history, read_stock_history_cache
from src.stock_data.inline_fetch import search_shortcut, offer_fetch
from src.date_controls import date_controls
from src.ui_experience import render_chart
from src.stock_data.units import canonical_turnover, turnover_gaps
from src.stock_data.turnover import turnover_issue_summary
from src.stock_analysis.charts import build_price_chart, build_return_chart
from src.stock_analysis.indicators import AnalysisInputError, calculate_indicators


DATA_DIR = Path("data/stocks")
CHART_OPTIONS = ["K线", "成交量", "成交额", "日收益率", "累计收益率", "回撤"]
PLOT_CONFIG = {"displaylogo": False, "scrollZoom": False, "toImageButtonOptions": {"format": "png", "scale": 2}}


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def _acquisition_link(key: str) -> str | None:
    return PAGE_DATA_ACQUISITION if st.button("前往数据获取 →", key=key, type="primary") else None


def render_visualization_page() -> str | None:
    left, middle, right = st.columns([2, 5, 2])
    if left.button("← 返回首页", key="return_home_visualization"):
        return PAGE_HOME
    middle.caption("本地缓存分析 · 页面交互不会自动联网")
    if right.button("进入策略回测 →", key="visualization_backtest"):
        return PAGE_BACKTEST
    st.title("金融数据可视化")
    search_shortcut("visualization")

    catalog, catalog_errors = list_cached_stock_history(DATA_DIR)
    if catalog_errors:
        with st.expander("部分本地缓存暂时无法读取"):
            for error in catalog_errors:
                st.warning(error)
    context = st.session_state.get("research_context", {})
    securities = {}
    for entry in catalog:
        securities.setdefault(f"{entry['asset_type']}:{entry['symbol']}", entry)
    selected_context = context.get("selected") or st.session_state.get("stock_selected")
    context_asset = context.get("asset_type") or st.session_state.get("stock_selected_type", "A股")
    context_key = f"{context_asset}:{selected_context['symbol']}" if selected_context else None
    if selected_context and context_key not in securities:
        securities[context_key] = {**selected_context, "asset_type": context_asset}
    if not securities:
        st.info("尚无本地行情缓存。请先获取一只 A 股或 ETF 的历史行情，再进入本页分析。")
        return _acquisition_link("visualization_no_cache")

    options = list(securities)
    if "visualization_security" not in st.session_state:
        st.session_state.visualization_security = context_key if context_key in options else options[0]
    if st.session_state.visualization_security not in options:
        st.session_state.visualization_security = options[0]
    security_column, adjustment_column, dates_column = st.columns([3, 2, 4])
    security_key = security_column.selectbox("本地证券（可输入代码或名称筛选）", options,
                                            format_func=lambda key: f"{securities[key]['asset_type']} · {securities[key]['symbol']} {securities[key].get('name', '')}",
                                            key="visualization_security")
    selected = securities[security_key]
    matches = [item for item in catalog if item["symbol"] == selected["symbol"] and item["asset_type"] == selected["asset_type"]]
    adjustment_options = [value for value in ("qfq", "hfq", "") if any(item["adjustment"] == value for item in matches)]
    if not adjustment_options:
        adjustment_options = [context.get("adjustment", "qfq")]
    if st.session_state.get("visualization_adjustment") not in adjustment_options:
        preference = context.get("adjustment", "qfq")
        st.session_state.visualization_adjustment = preference if preference in adjustment_options else adjustment_options[0]
    adjustment = adjustment_column.selectbox("缓存请求复权方式", adjustment_options,
                                             format_func=lambda value: ADJUSTMENT_LABELS[value], key="visualization_adjustment")
    entry = next((item for item in matches if item["adjustment"] == adjustment), {})
    if "visualization_dates" not in st.session_state:
        start_default = context.get("start") or entry.get("start") or (date.today() - timedelta(days=365)).isoformat()
        end_default = context.get("end") or entry.get("end") or date.today().isoformat()
        st.session_state.visualization_dates = (date.fromisoformat(start_default), date.fromisoformat(end_default))
    dates = date_controls("visualization_dates", *st.session_state.visualization_dates,
        all_start=date.fromisoformat(entry["start"]) if entry.get("start") else None,
        all_end=date.fromisoformat(entry["end"]) if entry.get("end") else None, selection_key=f"{security_key}:{adjustment}")
    if not isinstance(dates, (tuple, list)) or len(dates) != 2:
        st.info("请选择完整的开始、结束日期。")
        return None
    start, end = dates
    if start > end:
        st.error("开始日期不能晚于结束日期。")
        return None
    save_research_context(st.session_state, selected, selected["asset_type"], start, end, adjustment)
    try:
        result = read_stock_history_cache(selected["symbol"], start, end, adjustment=adjustment,
                                          asset_type=selected["asset_type"], data_dir=DATA_DIR)
    except StockDataError as exc:
        st.error(str(exc))
        return _acquisition_link("visualization_bad_cache")
    if result.data.empty:
        st.info("所选证券或日期区间没有缓存行情，不会自动下载。请调整日期或前往数据获取。")
        offer_fetch(selected, start, end, adjustment, DATA_DIR, "visualization")
        return _acquisition_link("visualization_empty_range")
    if result.status != "CACHE":
        st.warning("缓存未完整覆盖请求区间，以下只分析实际已保存的记录；可前往数据获取补齐。")
        offer_fetch(selected, start, end, adjustment, DATA_DIR, "visualization")
        if st.button("前往数据获取补齐", key="visualization_partial_cache"):
            return PAGE_DATA_ACQUISITION
    try:
        normalized, unit_warnings = canonical_turnover(result.data)
        analysis = calculate_indicators(normalized)
        analysis.warnings.extend(unit_warnings)
    except AnalysisInputError as exc:
        st.error(f"暂时无法计算：{exc}")
        return _acquisition_link("visualization_invalid_prices")
    data, metrics = analysis.data, analysis.metrics
    if data.empty:
        st.info("当前区间没有有效交易记录。请扩大区间或检查缓存。")
        return _acquisition_link("visualization_no_valid_rows")

    actual_modes = " / ".join(sorted(data["adjustment"].unique())) if "adjustment" in data else "未声明"
    sources = " / ".join(sorted(data["source"].unique())) if "source" in data else "未声明"
    updated = result.metadata.get("cache_updated_at") or result.metadata.get("updated_at") or "未声明"
    st.subheader(f"{selected.get('name') or selected['symbol']} · {selected['symbol']}")
    st.caption(f"{data['date'].iloc[0]:%Y-%m-%d} 至 {data['date'].iloc[-1]:%Y-%m-%d} · 实际复权：{actual_modes} · 来源状态：{result.status} / {sources} · 最近更新：{updated}")
    if result.metadata.get("price_fallback_rows", 0):
        st.info("腾讯返回的是原始 day 日线；本页按实际 none（不复权）价格计算，没有将其视为前/后复权价格。")
    for warning in analysis.warnings:
        st.warning(warning)
    issue_summary = turnover_issue_summary(result.metadata)
    if issue_summary:
        st.caption(issue_summary + " 成交量按逐行声明单位展示，来源与状态保留在导出结果中。")
    metric_columns = st.columns(5)
    for column, (label, value) in zip(metric_columns, [
        ("区间价格累计收益率", _percent(metrics["cumulative_return"])),
        ("年化波动率", _percent(metrics["annualized_volatility"])),
        ("最大回撤", _percent(metrics["max_drawdown"])),
        ("日涨跌幅平均值", _percent(metrics["mean_daily_return"])),
        ("有效交易日", str(metrics["valid_trading_days"])),
    ]):
        column.metric(label, value)
    st.info("平均日收益为正，区间最终收益仍可能为零或负数。")
    with st.expander("第一次看收益率？用一个例子理解"):
        st.markdown("**区间价格累计收益率**：最后收盘价 ÷ 区间首个有效收盘价 − 1，可理解为以起点收盘价为基准持有至终点的价格表现。\n\n**日涨跌幅平均值**：各次有效收盘价涨跌幅的算术平均，不等于累计收益除以天数。\n\n例如价格100→110→100，涨跌幅分别为+10%和约−9.09%，平均约+0.45%，但累计收益为0%。\n\n采用所选实际复权价格，不包含模拟手续费、滑点及完整分红现金流，不是精确账户收益。已识别的缺失交易日不计入日涨跌幅平均值和年化波动率；区间累计表现仍按实际起止价格计算。本地日历未覆盖时无法保证识别所有缺口。")
    st.caption("简单收益率 · 波动率采用样本标准差（ddof=1），按每年 252 个交易日年化 · 首个交易日收益率为空")

    preferences = st.session_state.get("visualization_preferences", {})
    settings, ma_settings = st.columns([4, 2])
    chart_choices = settings.multiselect("图表内容", CHART_OPTIONS, default=preferences.get("charts", [v for v in CHART_OPTIONS if v != "成交额"]), key="visualization_charts")
    ma_choices = ma_settings.multiselect("移动均线", [5, 20], default=preferences.get("ma", [5, 20]),
                                        format_func=lambda value: f"MA{value}", key="visualization_ma")
    st.session_state.visualization_preferences = {"charts": chart_choices, "ma": ma_choices}
    volume_unit = "份" if selected["asset_type"] == "ETF" else "股"
    if "K线" in chart_choices or "成交量" in chart_choices:
        render_chart(build_price_chart(data, show_price="K线" in chart_choices, show_volume="成交量" in chart_choices,
                                         moving_averages=tuple(ma_choices), volume_unit=volume_unit),
                        width="stretch", config=PLOT_CONFIG, key="visualization_price_chart")
    if "成交额" in chart_choices:
        from src.stock_analysis.charts import build_amount_chart
        render_chart(build_amount_chart(data), width="stretch", key="visualization_amount_chart")
    gaps = turnover_gaps(data)
    if not gaps.empty:
        with st.expander(f"成交字段待核验日期（{len(gaps)}天，缺少的字段在图中留空）"):
            st.caption("这些日期已有价格日线，不等于交易日缺失；法定休市日不会列入。")
            st.dataframe(gaps, hide_index=True, width="stretch")
    return_columns = st.columns(2)
    for container, label, field in zip(return_columns, ("日收益率", "累计收益率"), ("daily_return", "cumulative_return")):
        if label in chart_choices:
            with container:
                st.markdown(f"#### {label}")
                render_chart(build_return_chart(data, field), width="stretch", config=PLOT_CONFIG, key=f"visualization_{field}")
    if "回撤" in chart_choices:
        st.markdown("#### 回撤曲线")
        render_chart(build_return_chart(data, "drawdown"), width="stretch", config=PLOT_CONFIG, key="visualization_drawdown")
    if not chart_choices:
        st.info("请选择至少一种图表；指标结果仍可下载。")
    export = data.copy()
    export["date"] = export["date"].dt.strftime("%Y-%m-%d")
    export["trading_days_per_year"] = metrics["trading_days_per_year"]
    for field in ("cumulative_return", "annualized_volatility", "max_drawdown", "mean_daily_return", "valid_trading_days", "return_observations"):
        export[f"period_{field}"] = metrics[field]
    st.download_button("下载指标计算结果 CSV", export.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{selected['symbol']}_{start}_{end}_indicators.csv", mime="text/csv", key="visualization_export",
                       help="收益字段以小数保存，例如 0.05 表示 5%；包含实际复权方式、单位和来源。")
    with st.expander("指标定义与数据检查"):
        st.markdown(r"""
- 日收益率：$r_t=C_t/C_{t-1}-1$，首项为空；均值为有效 $r_t$ 的算术平均。
- MA：$MA_w(t)=\frac{1}{w}\sum_{i=0}^{w-1}C_{t-i}$，仅使用所选区间，窗口不足保留 NaN。
- 净值：$V_t=C_t/C_0$；累计收益率：$V_t-1$，区间首项为 0。
- 回撤：$D_t=V_t/\max_{s\leq t}V_s-1$；最大回撤：$-\min_t D_t$，摘要显示正的损失幅度。
- 年化波动率：$\operatorname{std}(r,ddof=1)\sqrt{252}$；少于 2 个收益观测不计算。
- 有效交易日为有效价格且非已知零成交的观测数；缺成交量时不推断停牌。日期空档不补齐；observation_return保留相邻有效记录变化，daily_return排除已识别缺失交易日的跨期收益，本地日历未覆盖时无法保证识别全部缺口。
- 缺失/异常价格、价格来源混用或实际复权方式混用会阻止计算；量额单位不明或缺失时仅该字段留空，保留价格分析及其他日期。
""")
        st.dataframe(export, hide_index=True, width="stretch")
    return None
