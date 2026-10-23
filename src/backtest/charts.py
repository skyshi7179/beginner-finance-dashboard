"""Backtest figures annotate the actual run's date range and assumptions."""

from html import escape

import plotly.graph_objects as go

from src.backtest.engine import BacktestResult
from src.stock_analysis.charts import apply_chart_layout, build_price_chart, UP, DOWN


def run_caption(result: BacktestResult, symbol: str) -> str:
    dates = result.equity["date"]
    params = result.params
    spec = result.metadata.get("strategy", {"kind":"ma"})
    detail = (f"MA{params.short_window}/{params.long_window}" if spec["kind"] == "ma" else
              f"通道窗口{spec['period']}/{spec['exit_period']}" if spec["kind"] == "channel" else
              f"RSI{spec['period']} 阈值{spec['lower']}/{spec['upper']}" if spec["kind"] == "rsi" else
              f"研究边界{spec['grid_lower']}/{spec['grid_upper']} 档数{spec['grid_levels']}" if spec["kind"] == "grid" else "自定义条件（见规则与假设导出）")
    return (f"{escape(result.metadata.get('strategy_label', '双均线策略'))} · {escape(symbol)} · {dates.iloc[0]:%Y-%m-%d}—{dates.iloc[-1]:%Y-%m-%d} · "
            f"{detail} · 费率{params.commission_rate:.2%} · 滑点{params.slippage_rate:.2%}"
            f"<br>初始研究资金 {params.initial_cash:,.0f} · 无风险年利率 {params.risk_free_rate:.1%}")


def build_portfolio_chart(result: BacktestResult, symbol: str, *, drawdown: bool = False) -> go.Figure:
    data = result.equity
    field = "drawdown" if drawdown else "nav"
    figure = go.Figure()
    for prefix, name, color in (("strategy", result.metadata.get("strategy_label", "双均线策略"), "#f0c65a"), ("benchmark", "买入持有", "#60a5fa")):
        figure.add_trace(go.Scatter(x=data["date"], y=data[f"{prefix}_{field}"], name=name, mode="lines",
                                    line=dict(color=color, width=2),
                                    hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2%}<extra>%{fullData.name}</extra>" if drawdown else "%{x|%Y-%m-%d}<br>净值 %{y:.4f}<extra>%{fullData.name}</extra>"))
    apply_chart_layout(figure, data, 310)
    figure.update_layout(title=dict(text=f"{'回撤曲线' if drawdown else '策略与基准净值'}<br><sup>{run_caption(result, symbol)}</sup>", font_size=14),
                         margin=dict(l=20, r=20, t=92, b=25), legend=dict(y=1.03))
    figure.update_yaxes(title_text="回撤" if drawdown else "净值（初始研究资金=1）", tickformat=".1%" if drawdown else ".2f")
    figure.add_hline(y=0 if drawdown else 1, line_dash="dot", line_color="rgba(255,255,255,.25)")
    return figure


def build_trade_chart(result: BacktestResult, symbol: str) -> go.Figure:
    data = result.equity
    figure = build_price_chart(data, show_volume=False, moving_averages=())
    for column, label, color in (("short_ma", f"MA{result.params.short_window}", "#f0c65a"),
                                  ("long_ma", f"MA{result.params.long_window}", "#60a5fa")):
        if result.metadata.get("strategy", {}).get("kind", "ma") != "ma":
            continue
        figure.add_trace(go.Scatter(x=data["date"], y=data[column], mode="lines", name=label,
                                    line=dict(color=color, width=1.4), connectgaps=False))
    for signal, name, color in ((1, "收盘金叉信号" if result.metadata.get("strategy", {}).get("kind", "ma") == "ma" else "收盘进入/增仓信号", "#f0c65a"), (-1, "收盘死叉信号" if result.metadata.get("strategy", {}).get("kind", "ma") == "ma" else "收盘退出/减仓信号", "#60a5fa")):
        rows = data[data["signal"].eq(signal)]
        figure.add_trace(go.Scatter(x=rows["date"], y=rows["close"], mode="markers", name=name,
                                    marker=dict(symbol="diamond-open", size=9, color=color),
                                    hovertemplate="%{x|%Y-%m-%d}<br>收盘信号，不在当日成交<extra></extra>"))
    for side, name, color, marker in (("BUY", "次日开盘模拟买入", UP, "triangle-up"), ("SELL", "次日开盘模拟卖出", DOWN, "triangle-down")):
        rows = result.trades[result.trades["side"].eq(side)]
        figure.add_trace(go.Scatter(x=rows["execution_date"], y=rows["execution_price"], mode="markers", name=name,
                                    marker=dict(symbol=marker, size=13, color=color),
                                    customdata=rows[["quantity", "commission"]].to_numpy(),
                                    hovertemplate="%{x|%Y-%m-%d}<br>研究成交价 %{y:.3f}<br>模拟数量 %{customdata[0]:,.0f}<br>模拟手续费 %{customdata[1]:.3f}<extra></extra>"))
    figure.update_layout(height=530, title=dict(text=f"K线、策略信号与模拟成交<br><sup>{run_caption(result, symbol)}</sup>", font_size=14),
                         margin=dict(l=20, r=20, t=110, b=25), legend=dict(y=1.0, yanchor="bottom", orientation="h"))
    figure.update_yaxes(title_text="研究价格（首日开盘=100）")
    return figure
