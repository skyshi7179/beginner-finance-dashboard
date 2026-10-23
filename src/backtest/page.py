"""Explicit-run Streamlit interface; selection reads cache and never downloads."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from src.navigation import PAGE_HOME, PAGE_VISUALIZATION, PAGE_DATA_ACQUISITION
from src.stock_data.context import ADJUSTMENT_LABELS, save_research_context
from src.stock_data.errors import StockDataError
from src.stock_data.workspace import list_cached_stock_history, read_stock_history_cache
from src.stock_data.inline_fetch import search_shortcut, offer_fetch
from src.date_controls import date_controls
from src.ui_experience import render_chart
from src.backtest.strategy_controls import strategy_controls
from src.backtest.engine import BacktestInputError, BacktestParams, run_backtest
from src.backtest.charts import build_portfolio_chart, build_trade_chart
from src.backtest.export import export_run


DATA_DIR = Path("data/stocks")
PLOT_CONFIG = {"displaylogo": False, "scrollZoom": False}
PERFORMANCE_LABELS = {"cumulative_return": "累计收益率", "annualized_return": "年化收益率",
                      "annualized_volatility": "年化波动率", "max_drawdown": "最大回撤",
                      "sharpe_ratio": "夏普比率", "trade_count": "成交次数"}
ORDER_LABELS = {"signal_date": "收盘信号日期", "scheduled_date": "计划开盘日期", "execution_date": "模拟成交日期",
                "side": "方向", "status": "订单状态", "reason": "原因", "quantity": "模拟数量（非真实股数）",
                "reference_open": "参考研究开盘价", "execution_price": "含滑点研究成交价", "gross_value": "模拟成交额",
                "commission": "手续费（研究单位）", "slippage_cost": "滑点损耗（研究单位）",
                "cash_after": "成交后研究现金", "shares_after": "成交后模拟持仓"}


def _percent(value) -> str:
    return "—" if pd.isna(value) else f"{value:.2%}"


def _get_data(key: str) -> str | None:
    return PAGE_DATA_ACQUISITION if st.button("前往数据获取 →", type="primary", key=key) else None


def render_backtest_page() -> str | None:
    left, middle, right = st.columns([2, 5, 2])
    if left.button("← 返回首页", key="return_home_backtest"):
        return PAGE_HOME
    middle.caption("单证券 · 仅做多 · 收盘信号 / 下一日线开盘执行")
    if right.button("返回可视化分析 →", key="backtest_visualization"):
        return PAGE_VISUALIZATION
    st.title("自定义策略回测")
    search_shortcut("backtest")
    st.warning("归一化研究回测：首日开盘设为100。资金、持仓和交易费用均为模拟研究单位，不是人民币、真实股数或真实券商成本；复权价格不还原分红、拆股等公司行为。")
    catalog, errors = list_cached_stock_history(DATA_DIR)
    if errors:
        with st.expander("本地缓存读取提示"):
            st.write(errors)
    context = st.session_state.get("research_context", {})
    securities = {}
    for item in catalog:
        securities.setdefault(f"{item['asset_type']}:{item['symbol']}", item)
    current = context.get("selected") or st.session_state.get("stock_selected")
    current_asset = context.get("asset_type") or st.session_state.get("stock_selected_type", "A股")
    current_key = f"{current_asset}:{current['symbol']}" if current else None
    if current and current_key not in securities:
        securities[current_key] = {**current, "asset_type": current_asset}
    if not securities:
        st.info("尚无本地缓存，请先获取历史行情。回测页不会自动下载数据。")
        return _get_data("backtest_no_cache")
    options = list(securities)
    if st.session_state.get("backtest_security") not in options:
        st.session_state.backtest_security = current_key if current_key in options else options[0]
    stock_column, mode_column, date_column = st.columns([3, 2, 4])
    key = stock_column.selectbox("选择本地证券", options, key="backtest_security",
                                 format_func=lambda value: f"{securities[value]['asset_type']} · {securities[value]['symbol']} {securities[value].get('name', '')}")
    selected = securities[key]
    matches = [item for item in catalog if item["symbol"] == selected["symbol"] and item["asset_type"] == selected["asset_type"]]
    modes = [mode for mode in ("qfq", "hfq", "") if any(item["adjustment"] == mode for item in matches)] or [context.get("adjustment", "qfq")]
    if st.session_state.get("backtest_adjustment") not in modes:
        preferred = context.get("adjustment", "qfq")
        st.session_state.backtest_adjustment = preferred if preferred in modes else modes[0]
    adjustment = mode_column.selectbox("缓存请求复权", modes, format_func=lambda mode: ADJUSTMENT_LABELS[mode], key="backtest_adjustment")
    entry = next((item for item in matches if item["adjustment"] == adjustment), {})
    if "backtest_dates" not in st.session_state:
        st.session_state.backtest_dates = (date.fromisoformat(context.get("start") or entry.get("start") or (date.today() - timedelta(days=365)).isoformat()),
                                           date.fromisoformat(context.get("end") or entry.get("end") or date.today().isoformat()))
    dates = date_controls("backtest_dates", *st.session_state.backtest_dates,
        all_start=date.fromisoformat(entry["start"]) if entry.get("start") else None,
        all_end=date.fromisoformat(entry["end"]) if entry.get("end") else None, selection_key=f"{key}:{adjustment}")
    if not isinstance(dates, (tuple, list)) or len(dates) != 2:
        st.info("请选择完整日期区间。")
        return None
    start, end = dates
    if start > end:
        st.error("开始日期不能晚于结束日期。")
        return None
    save_research_context(st.session_state, selected, selected["asset_type"], start, end, adjustment)
    try:
        cached = read_stock_history_cache(selected["symbol"], start, end, adjustment=adjustment,
                                          asset_type=selected["asset_type"], data_dir=DATA_DIR)
    except StockDataError as exc:
        st.error(str(exc))
        return _get_data("backtest_cache_error")
    if cached.data.empty:
        st.info("当前区间没有缓存行情，不会自动请求网络。")
        offer_fetch(selected, start, end, adjustment, DATA_DIR, "backtest")
        return _get_data("backtest_empty_cache")
    if cached.status != "CACHE":
        st.warning("缓存未完整覆盖请求日期，回测只使用实际已保存记录；日期间隔规则仍然生效。")
        offer_fetch(selected, start, end, adjustment, DATA_DIR, "backtest")
    actual = " / ".join(sorted(cached.data["adjustment"].unique()))
    sources = " / ".join(sorted(cached.data["source"].unique()))
    updated = cached.metadata.get("cache_updated_at") or cached.metadata.get("updated_at") or "未声明"
    st.caption(f"{selected.get('name') or selected['symbol']} · {selected['symbol']} · {len(cached.data)} 条 · 实际复权 {actual} · {cached.status} / {sources} · 更新 {updated}")
    with st.expander("执行、交易成本及数据假设"):
        st.markdown("""
- 首日持仓为0、无待执行订单，初始研究现金由下方参数指定。指标仅用所选样本，不预先建仓；双均线策略要求真实交叉，其他策略按所选条件判断。
- 所选策略收盘确认条件（双均线为金叉/死叉）；下一条经本地日历确认的交易日开盘执行。国庆等休市长假不会仅因自然日间隔较长而取消；若中间确有应开市日缺记录、日历覆盖未知，或下一日停牌/零成交/价格无效，则取消且不顺延。
- 买入按100个模拟单位向下取整，资金包含手续费后必须充足；卖出一次清空全部持仓（含剩余零股的模拟规则）。不覆盖不同板块及 ETF 的完整交易规则。
- 手续费双向按模拟成交额比例收取，无最低手续费；滑点买入加价、卖出减价。未模拟印花税、过户费、涨跌停、真实流动性和公司行为。
- 买入持有基准在首个可交易开盘以相同费率、滑点和整手规则买入，剩余现金不计息；策略与基准末日均不强制平仓，按收盘估值。
- 最后一个收盘信号无下一日线时记为未成交；成交次数为已成交委托数（买、卖分别计数），不等于完整往返次数。
- 无风险利率默认年化0%，波动率/夏普采用252交易日约定。成交量用于可交易检查，也可作为自定义信号条件；不用于限制模拟订单容量。
- 缓存复权价可能被数据源事后重算，并非逐时点价格快照；引擎不读取未来行，但无法证明上游数据没有历史修订。
""")
    spec = strategy_controls()
    from src.backtest.learning import COMMON_GUIDE
    with st.expander("资金、手续费、滑点怎么填写？"):
        st.markdown(COMMON_GUIDE)
    preferences = st.session_state.get("backtest_preferences", {})
    with st.form("backtest_parameter_form"):
        a, b, c = st.columns(3)
        if st.session_state.get("strategy_kind", "ma") == "ma":
            short = int(a.number_input("短期均线窗口", 1, 249, value=preferences.get("short_window", 5), key="backtest_short", help="5表示最近5个交易观测收盘价的均值，通常更敏感；默认5不是最佳参数。"))
            long = int(b.number_input("长期均线窗口", 2, 250, value=preferences.get("long_window", 20), key="backtest_long", help="20表示最近20个交易观测；作为较长参照，必须大于短期窗口。"))
        else:
            short, long = 5, 20
            a.caption("规则参数使用上方所选策略配置")
            b.caption("每个收盘形成信号，下一开盘模拟执行")
        capital = c.number_input("初始资金（研究单位）", min_value=1000., max_value=1e9, value=preferences.get("initial_cash", 100000.), step=10000., key="backtest_capital", help="模拟研究账户起始资金，不是实际人民币；影响可买数量及100模拟单位限制。")
        a, b, c = st.columns(3)
        fee = a.number_input("双向手续费率（%）", 0., 1., value=preferences.get("commission_rate", .0003) * 100, step=.01, format="%.3f", key="backtest_fee", help="填0.03表示0.03%，不是3%；模拟成交额10000时费用3，买卖双向收取。")
        slip = b.number_input("单边滑点（%）", 0., 5., value=preferences.get("slippage_rate", .0005) * 100, step=.01, format="%.3f", key="backtest_slippage", help="填0.05表示0.05%。参考价100时，模拟买入100.05、卖出99.95。")
        c.caption("无风险利率：年化0%\n\n价格基准：首日开盘=100")
        clicked = st.form_submit_button("运行回测", type="primary", disabled=spec is None)
    params = BacktestParams(short, long, float(capital), float(fee) / 100, float(slip) / 100)
    st.session_state.backtest_preferences = asdict(params)
    digest = hashlib.sha256(pd.util.hash_pandas_object(cached.data, index=False).values.tobytes()).hexdigest()
    signature = repr((key, adjustment, start.isoformat(), end.isoformat(), asdict(params), digest, asdict(spec) if spec else None))
    if clicked:
        st.session_state.pop("backtest_run_result", None)
        try:
            with st.spinner("正在计算收盘交叉、下一开盘成交及逐日账户…"):
                st.session_state.backtest_run_result = run_backtest(cached.data, params, strategy=spec)
            st.session_state.backtest_run_signature = signature
        except (BacktestInputError, ValueError) as exc:
            st.error(f"无法运行回测：{exc}")
    result = st.session_state.get("backtest_run_result")
    if result is None or st.session_state.get("backtest_run_signature") != signature:
        st.info("选择参数后点击“运行回测”。修改证券、区间、参数或缓存后，需重新运行；页面重绘不会自动回测或下载。")
        return None
    st.subheader("回测结果 · 归一化研究模型")
    st.caption("以下结果对应上次点击“运行回测”时提交的参数；表单中的未提交修改不会更新结果，各图注明实际使用参数。")
    for warning in result.warnings[1:]:
        st.warning(warning)
    strategy = result.metrics.iloc[0]
    benchmark = result.metrics.loc["买入持有"]
    a, b, c, d = st.columns(4)
    a.metric("策略累计收益", _percent(strategy["cumulative_return"]))
    b.metric("买入持有收益", _percent(benchmark["cumulative_return"]))
    c.metric("策略最大回撤", _percent(strategy["max_drawdown"]))
    d.metric("策略成交次数", str(int(strategy["trade_count"])))
    a, b = st.columns(2)
    with a:
        render_chart(build_portfolio_chart(result, selected["symbol"]), width="stretch", config=PLOT_CONFIG, key="backtest_nav_chart")
    with b:
        render_chart(build_portfolio_chart(result, selected["symbol"], drawdown=True), width="stretch", config=PLOT_CONFIG, key="backtest_dd_chart")
    render_chart(build_trade_chart(result, selected["symbol"]), width="stretch", config=PLOT_CONFIG, key="backtest_trade_chart")
    st.markdown("#### 绩效对比")
    display = result.metrics[list(PERFORMANCE_LABELS)].copy()
    for field in PERFORMANCE_LABELS:
        display[field] = display[field].map(lambda value: str(int(value)) if field == "trade_count" else "—" if pd.isna(value) else f"{value:.3f}" if field == "sharpe_ratio" else _percent(value))
    st.dataframe(display.rename(columns=PERFORMANCE_LABELS), width="stretch")
    trade_tab, order_tab, account_tab = st.tabs(["已成交记录", "全部信号订单（含取消/未成交）", "逐日现金与持仓"])
    with trade_tab:
        st.dataframe(result.trades.rename(columns=ORDER_LABELS), hide_index=True, width="stretch")
        st.caption("数量、成交价、成交额及费用使用研究单位；开仓后未平仓的收益包含未实现盈亏。")
    with order_tab:
        st.dataframe(result.orders.rename(columns=ORDER_LABELS), hide_index=True, width="stretch")
        with st.expander("买入持有基准订单"):
            st.dataframe(result.benchmark_orders.rename(columns=ORDER_LABELS), hide_index=True, width="stretch")
    with account_tab:
        columns = ["date", "signal", "strategy_cash", "strategy_shares", "strategy_equity", "strategy_nav", "benchmark_nav", "pending_side", "strategy_cost_basis", "position_return", "holding_days", "rule_status"]
        st.dataframe(result.equity[columns], hide_index=True, width="stretch")
    strategy_tag = f"MA{short}-{long}" if result.metadata["strategy"]["kind"] == "ma" else result.metadata["strategy"]["kind"]
    stem = f"{selected['symbol']}_{adjustment or 'none'}_{strategy_tag}_{start}_{end}"
    export_context = {"symbol": selected["symbol"], "name": selected.get("name", ""), "asset_type": selected["asset_type"],
                      "start": start.isoformat(), "end": end.isoformat(), "requested_adjustment": adjustment or "none",
                      "actual_adjustment": actual, "source": sources, "cache_status": cached.status, "cache_updated_at": updated,
                      "data_digest": digest}
    a, b, c = st.columns(3)
    a.download_button("每日账户 CSV", result.equity.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8-sig"), file_name=f"{stem}_equity.csv", mime="text/csv", key="backtest_export_equity")
    b.download_button("成交记录 CSV", result.trades.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8-sig"), file_name=f"{stem}_trades.csv", mime="text/csv", key="backtest_export_trades")
    c.download_button("完整回测 ZIP", export_run(result, export_context), file_name=f"{stem}.zip", mime="application/zip", key="backtest_export_all")
    with st.expander("绩效公式与边界"):
        st.markdown(r"""
净值 $V_t=E_t/E_0$，其中 $E_t=现金_t+模拟持仓_t\times研究收盘价_t$，$E_0$ 为交易前初始研究资金。

日收益 $r_t=V_t/V_{t-1}-1$，首项为 $V_0-1$，计入首日开盘成本及当日价格变化。

累计收益 $V_T-1$；年化收益 $V_T^{252/N}-1$，$N$ 为输入日线观测数；不是自然日年化。

年化波动率 $std(r,ddof=1)\sqrt{252}$；夏普 $\sqrt{252}\,mean(r-r_{f,d})/std(r,ddof=1)$，$r_{f,d}=(1+r_f)^{1/252}-1$，默认 $r_f=0$；波动为0时夏普为空。

最大回撤 $\max_t(1-V_t/\max(1,V_0,\ldots,V_t))$，初始资金1也作为历史峰值，所以首日手续费不会从回撤中消失。

均线只看当日及历史收盘，下一开盘只用于成交。未使用未来价格计算历史信号；没有公司行为和真实市场微观结构模型，结果不能等同于可实盘执行收益。
""")
    return None
