"""An explicit-generation Streamlit workspace. Navigation never calls a model."""

from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from src.ai_report.data import REPORT_TYPES, ReportInputError, build_analysis, input_signature
from src.ai_report.settings import load_models
from src.ai_report.prompts import PROMPT_VERSION
from src.ai_report.service import ReportBusyError, generate_report
from src.backtest.engine import BacktestParams
from src.navigation import PAGE_HOME, PAGE_DATA_ACQUISITION
from src.stock_data.context import ADJUSTMENT_LABELS, save_research_context
from src.stock_data.errors import StockDataError
from src.stock_data.workspace import list_cached_stock_history, read_stock_history_cache
from src.stock_data.symbols import search_stocks
from src.time_utils import get_china_now


DATA_DIR = Path("data/stocks")
LOCAL_MODEL = "local_rules"


def _acquisition(key):
    return PAGE_DATA_ACQUISITION if st.button("前往股票数据获取 →", key=key, type="primary") else None


def render_ai_report_page() -> str | None:
    if st.button("← 返回首页", key="return_home_ai_report"):
        return PAGE_HOME
    st.title("研究报告")
    st.write("选择股票及分析区间，使用历史数据和量化指标生成研究摘要；可选本地规则或已配置AI模型。")
    st.caption("优先读取本地缓存 · 仅点击生成按钮后计算指标与回测、调用已配置模型 · 不自动下载行情")
    preferences = st.session_state.get("ai_report_preferences", {})
    context = st.session_state.get("research_context", {})
    catalog, errors = list_cached_stock_history(DATA_DIR)
    if errors:
        with st.expander("部分本地缓存无法读取"):
            st.write(errors)
    if "ai_report_search" not in st.session_state:
        st.session_state.ai_report_search = preferences.get("search", "")
    query = st.text_input("搜索已缓存证券（股票代码或中文名称）", key="ai_report_search", placeholder="例如600519、贵州茅台或ETF名称")
    securities = {}
    for entry in catalog:
        securities.setdefault(f"{entry['asset_type']}:{entry['symbol']}", entry)
    if not securities:
        st.info("尚无本地行情缓存。请先搜索并获取股票或ETF，再返回生成报告。")
        return _acquisition("ai_report_no_cache")
    if query.strip():
        matches = search_stocks(query, pd.DataFrame(securities.values()))
        codes = set(matches["symbol"].astype(str))
        options = [key for key, item in securities.items() if item["symbol"] in codes]
    else:
        options = list(securities)
    if not options:
        st.info("本地缓存未找到该代码或名称；这不代表证券不存在。可前往股票数据获取搜索完整代码表。")
        return _acquisition("ai_report_search_missing")
    current = context.get("selected", {})
    context_key = f"{context.get('asset_type', 'A股')}:{current.get('symbol', '')}"
    if st.session_state.get("ai_report_security") not in options:
        preferred = context_key if context_key in options else preferences.get("security")
        st.session_state.ai_report_security = preferred if preferred in options else options[0]
    security_column, adjustment_column = st.columns([3, 2])
    key = security_column.selectbox("选择已缓存股票 / ETF", options, key="ai_report_security",
                                   format_func=lambda value: f"{securities[value]['asset_type']} · {securities[value]['symbol']} {securities[value]['name']}")
    selected = securities[key]
    entries = [entry for entry in catalog if entry["symbol"] == selected["symbol"] and entry["asset_type"] == selected["asset_type"]]
    modes = [mode for mode in ("qfq", "hfq", "") if any(entry["adjustment"] == mode for entry in entries)]
    if st.session_state.get("ai_report_adjustment") not in modes:
        preferred = context.get("adjustment", preferences.get("adjustment", "qfq"))
        st.session_state.ai_report_adjustment = preferred if preferred in modes else modes[0]
    adjustment = adjustment_column.selectbox("缓存请求复权方式", modes, key="ai_report_adjustment", format_func=lambda value: ADJUSTMENT_LABELS[value])
    entry = next(entry for entry in entries if entry["adjustment"] == adjustment)
    if "ai_report_dates" not in st.session_state:
        fallback = preferences.get("dates", [])
        begin = context.get("start") or (fallback[0] if fallback else None) or entry.get("start") or (get_china_now().date() - timedelta(days=365)).isoformat()
        end = context.get("end") or (fallback[1] if fallback else None) or entry.get("end") or get_china_now().date().isoformat()
        st.session_state.ai_report_dates = (date.fromisoformat(begin), date.fromisoformat(end))
    dates = st.date_input("分析日期区间", key="ai_report_dates")
    if not isinstance(dates, (tuple, list)) or len(dates) != 2:
        st.info("请选择完整分析日期区间。")
        return None
    start, end = dates
    if start > end:
        st.error("开始日期不能晚于结束日期。")
        return None
    save_research_context(st.session_state, selected, selected["asset_type"], start, end, adjustment)
    report_column, model_column = st.columns([3, 2])
    if st.session_state.get("ai_report_type") not in REPORT_TYPES:
        st.session_state.ai_report_type = preferences.get("report_type", "comprehensive")
    report_type = report_column.selectbox("报告类型", list(REPORT_TYPES), key="ai_report_type", format_func=lambda value: REPORT_TYPES[value])
    models, model_errors = load_models()
    model_map = {model.identity: model for model in models}
    model_options = [LOCAL_MODEL, *model_map]
    if st.session_state.get("ai_report_model") not in model_options:
        preferred = preferences.get("model", next(iter(model_map), LOCAL_MODEL))
        st.session_state.ai_report_model = preferred if preferred in model_options else LOCAL_MODEL
    model_key = model_column.selectbox("报告生成模型", model_options, key="ai_report_model",
                                       format_func=lambda value: "本地规则模板（无需API）" if value == LOCAL_MODEL else f"{model_map[value].display_name}（已配置，调用时验证）")
    model = model_map.get(model_key)
    for error in model_errors:
        st.warning(error)
    if not models:
        st.info("未配置可调用的LLM连接；点击生成后将自动使用本地规则模板，不调用外部模型。")
    elif model is not None:
        st.caption("列表只含完整配置的模型；可用性在点击生成时验证，不提前调用API。生成会向该模型发送结构化统计JSON；模型仅选择重点和组织受控句式。")
    backtest_preferences = preferences.get("backtest") or st.session_state.get("backtest_preferences", {})
    params = BacktestParams(**backtest_preferences) if backtest_preferences else BacktestParams()
    if report_type in {"backtest", "comprehensive"}:
        with st.expander("双均线回测参数与研究口径", expanded=False):
            a, b, c = st.columns(3)
            short = int(a.number_input("短期窗口", 1, 249, value=params.short_window, key="ai_report_short"))
            long = int(b.number_input("长期窗口", 2, 250, value=params.long_window, key="ai_report_long"))
            capital = c.number_input("初始资金（研究单位）", 1000., 1e9, value=params.initial_cash, step=10000., key="ai_report_capital")
            a, b = st.columns(2)
            fee = a.number_input("双向手续费率（%）", 0., 1., value=params.commission_rate * 100, step=.01, format="%.3f", key="ai_report_fee")
            slip = b.number_input("单边滑点（%）", 0., 5., value=params.slippage_rate * 100, step=.01, format="%.3f", key="ai_report_slippage")
            params = BacktestParams(short, long, float(capital), float(fee) / 100, float(slip) / 100)
            st.warning("回测使用首日开盘=100的研究单位；收盘信号下一日线开盘执行，不是人民币现金流或精确实盘模拟。无风险年利率默认0%。")
    st.session_state.ai_report_preferences = {"search": query, "security": key, "adjustment": adjustment,
                                             "dates": [start.isoformat(), end.isoformat()], "report_type": report_type,
                                             "model": model_key, "backtest": asdict(params)}
    try:
        cached = read_stock_history_cache(selected["symbol"], start, end, adjustment=adjustment, asset_type=selected["asset_type"], data_dir=DATA_DIR)
    except StockDataError as exc:
        st.error(str(exc))
        return _acquisition("ai_report_cache_error")
    if cached.data.empty:
        st.info("所选区间无有效缓存行情，请先前往数据获取；本页不下载。")
        return _acquisition("ai_report_empty_range")
    if cached.status != "CACHE":
        st.warning("缓存未覆盖完整请求区间；生成时仅分析实际已有数据，并在报告中披露覆盖限制。")
        if st.button("前往数据获取补齐 →", key="ai_report_partial"):
            return PAGE_DATA_ACQUISITION
    st.caption(f"{selected['name']} · {selected['symbol']} · 缓存{len(cached.data)}条 · {cached.status} · 更新{cached.metadata.get('cache_updated_at') or cached.metadata.get('updated_at') or '未声明'}")
    signature = input_signature(cached, selected, start.isoformat(), end.isoformat(), adjustment, report_type, params, model_key + PROMPT_VERSION)
    result = st.session_state.get("ai_report_result")
    matches = result is not None and st.session_state.get("ai_report_signature") == signature
    generate_column, regenerate_column = st.columns([3, 2])
    generate = generate_column.button("生成研究报告", type="primary", key="ai_report_generate", width="stretch")
    regenerate = regenerate_column.button("重新生成", key="ai_report_regenerate", disabled=not matches, width="stretch")
    if generate and matches:
        st.info("当前参数与缓存未改变，展示已生成报告；如需再次调用模型，请点击“重新生成”。")
    elif generate or regenerate:
        st.session_state.pop("ai_report_result", None)
        result = None
        try:
            with st.status("正在计算指标、准备结构化事实并生成核验报告…", expanded=True) as status:
                packet = build_analysis(cached, selected, start=start.isoformat(), end=end.isoformat(), adjustment=adjustment, report_type=report_type, params=params)
                result = generate_report(packet, model)
                st.session_state.ai_report_result = result
                st.session_state.ai_report_signature = signature
                status.update(label="报告已生成" if not result.fallback_reason else "已生成规则回退报告", state="complete", expanded=False)
            st.rerun()  # Refresh button availability; the matching signature prevents a second call.
        except (ReportInputError, StockDataError, ReportBusyError) as exc:
            st.error(f"暂时无法生成报告：{exc}")
    if result is None or st.session_state.get("ai_report_signature") != signature:
        st.info("点击生成按钮开始。证券、日期、复权、报告类型、模型、回测参数或缓存变化后，旧报告会隐藏，需要重新生成。")
        return None
    if result.mode == "LLM_VERIFIED_PLAN":
        st.success("已生成LLM辅助报告：结构、身份、日期、适用句式和引用值通过本地核验。")
    elif result.mode == "RULES_FALLBACK":
        st.warning(f"模型调用或输出核验未通过，已回退本地规则报告：{result.fallback_reason}")
    else:
        st.info("已生成本地规则报告；没有调用LLM API。")
    st.markdown(result.markdown)
    filename = f"{selected['symbol']}_{start}_{end}_{report_type}_{result.payload['analysis_id'][:8]}"
    a, b = st.columns(2)
    a.download_button("下载Markdown", result.markdown.encode("utf-8"), file_name=filename + ".md", mime="text/markdown", key="ai_report_download_md")
    b.download_button("下载分析指标CSV", result.metrics_csv, file_name=filename + ".csv", mime="text/csv", key="ai_report_download_csv")
    with st.expander("核验依据与结构化数据"):
        st.write(result.validation)
        st.caption("不接受无法核验的模型自由文本数字。输入事实的业务真实性仍取决于原缓存来源与上游质量。")
        st.json(result.payload, expanded=False)
        st.json(result.plan, expanded=False)
    return None
