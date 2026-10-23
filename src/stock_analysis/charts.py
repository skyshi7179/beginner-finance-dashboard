"""Plotly rendering consumes calculated data and never requests a provider."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.chart_theme import DOWN_COLOR, UP_COLOR


UP = UP_COLOR
DOWN = DOWN_COLOR
MA_COLORS = {5: "#f0c65a", 20: "#60a5fa"}


def apply_chart_layout(fig: go.Figure, data: pd.DataFrame, height: int) -> go.Figure:
    revision = f"{data['date'].min()}:{data['date'].max()}" if not data.empty else "empty"
    fig.update_layout(template="plotly_dark", paper_bgcolor="#050505", plot_bgcolor="#050505",
                      height=height, margin=dict(l=20, r=20, t=35, b=25),
                      font=dict(family="Arial, PingFang SC, sans-serif", color="#d1d5db"),
                      hovermode="x unified", dragmode="zoom", uirevision=revision,
                      legend=dict(orientation="h", y=1.08, x=0),
                      hoverlabel=dict(namelength=-1))
    fig.update_xaxes(type="date", rangebreaks=[dict(bounds=["sat", "mon"])], showgrid=False)
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.07)", zeroline=False)
    if data.empty:
        fig.add_annotation(text="暂无有效数据", x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
    return fig


def build_price_chart(data: pd.DataFrame, *, show_price: bool = True, show_volume: bool = True,
                      moving_averages: tuple[int, ...] = (5, 20), volume_unit: str = "股") -> go.Figure:
    rows = 2 if show_price and show_volume else 1
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=.035,
                        row_heights=[.72, .28] if rows == 2 else [1])
    if not data.empty and show_price:
        hover = [f"{row.date:%Y-%m-%d}<br>开盘 {row.open:.3f}<br>最高 {row.high:.3f}<br>最低 {row.low:.3f}<br>收盘 {row.close:.3f}"
                 for row in data.itertuples()]
        fig.add_trace(go.Candlestick(x=data["date"], open=data["open"], high=data["high"],
                                     low=data["low"], close=data["close"], name="K线",
                                     increasing_line_color=UP, decreasing_line_color=DOWN,
                                     text=hover, hoverinfo="text"), row=1, col=1)
        for window in moving_averages:
            if window in MA_COLORS:
                fig.add_trace(go.Scatter(x=data["date"], y=data[f"ma{window}"], name=f"MA{window}",
                                         mode="lines", connectgaps=False,
                                         line=dict(color=MA_COLORS[window], width=1.5),
                                         hovertemplate=f"MA{window} %{{y:.3f}}<extra></extra>"), row=1, col=1)
        fig.update_yaxes(title_text="价格（元）", row=1, col=1)
    if not data.empty and show_volume:
        maximum = data["volume"].max()
        factor = 10000. if pd.notna(maximum) and maximum >= 10000 else 1.
        display_volume_unit = f"万{volume_unit}" if factor > 1 else volume_unit
        colors = [UP if close >= opened else DOWN for close, opened in zip(data["close"], data["open"])]
        fig.add_trace(go.Bar(x=data["date"], y=data["volume"] / factor, marker_color=colors, name=f"成交量（{display_volume_unit}）",
                             hovertemplate=f"%{{x|%Y-%m-%d}}<br>成交量 %{{y:,.2f}} {display_volume_unit}<extra></extra>"), row=rows, col=1)
        missing = data[data["volume"].isna()]
        if not missing.empty:
            fig.add_trace(go.Scatter(x=missing["date"], y=[0.] * len(missing), mode="markers",
                marker=dict(symbol="x", color="#94a3b8", size=8), name="成交量缺失（非零成交）",
                hovertemplate="%{x|%Y-%m-%d}<br>成交量缺失或单位未确认；未补零<extra></extra>"), row=rows, col=1)
        fig.update_yaxes(title_text=f"成交量（{display_volume_unit}）", tickformat=",.1f" if factor > 1 else ",.0f", row=rows, col=1)
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=.08), row=rows, col=1)
    fig.update_xaxes(
        title=dict(text="时间范围缩略图：拖动白色把手选择查看区间", standoff=8),
        row=rows,
        col=1,
    )
    return apply_chart_layout(fig, data, 510 if rows == 2 else 390)


def build_return_chart(data: pd.DataFrame, column: str) -> go.Figure:
    titles = {"daily_return": "日收益率", "cumulative_return": "累计收益率", "drawdown": "回撤"}
    title = titles[column]
    fig = go.Figure()
    if not data.empty:
        if column == "daily_return":
            fig.add_trace(go.Bar(x=data["date"], y=data[column], name=title,
                                 marker_color=[UP if value >= 0 else DOWN for value in data[column]],
                                 hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"))
        else:
            fig.add_trace(go.Scatter(x=data["date"], y=data[column], name=title, mode="lines",
                                     line=dict(color="#60a5fa" if column == "cumulative_return" else DOWN, width=2),
                                     fill="tozeroy" if column == "drawdown" else None,
                                     hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"))
    fig.update_yaxes(tickformat=".1%", title_text=title)
    fig.add_hline(y=0, line_color="rgba(255,255,255,.25)", line_width=1)
    return apply_chart_layout(fig, data, 280)


def build_amount_chart(data: pd.DataFrame) -> go.Figure:
    """Amount in CNY, retaining missing dates as empty bar positions."""
    fig = go.Figure()
    unit, factor = "元", 1.
    if not data.empty:
        maximum=data["amount"].max()
        if pd.notna(maximum) and maximum >= 1e8:unit,factor="亿元",1e8
        elif pd.notna(maximum) and maximum >= 1e4:unit,factor="万元",1e4
        fig.add_trace(go.Bar(x=data["date"], y=data["amount"] / factor, name=f"成交额（{unit}）",
            hovertemplate=f"%{{x|%Y-%m-%d}}<br>成交额 %{{y:,.2f}} {unit}<extra></extra>", marker_color="#60a5fa"))
        missing = data[data["amount"].isna()]
        if not missing.empty:
            fig.add_trace(go.Scatter(x=missing["date"], y=[0.] * len(missing), mode="markers",
                marker=dict(symbol="x", color="#94a3b8"), name="成交额缺失（非零成交）",
                hovertemplate="%{x|%Y-%m-%d}<br>成交额缺失或单位未确认；未补零<extra></extra>"))
    fig.update_yaxes(title_text=f"成交额（{unit}）",tickformat=",.2f")
    return apply_chart_layout(fig, data, 280)
