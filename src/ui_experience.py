"""Shared chart interaction, native theme adaptation, and temporary leases."""
from pathlib import Path
import re
import streamlit as st
import plotly.graph_objects as go

from src.stock_data.session_cache import touch, start_cleaner


def theme_css(css: str) -> str:
    # Global Streamlit pages do not expose component-v2 theme variables.
    # Inherit native foregrounds and use translucent currentColor panels, so
    # the frontend can switch themes immediately without a Python rerun.
    css = re.sub(r'\s*(?:\.stApp|\[data-testid="stSidebar"\]|\[data-testid="stHeader"\])\s*\{[^}]*\}', '', css)
    mappings = {
        "#050505": "var(--background-color)", "#080808": "var(--secondary-background-color)",
        "#0b0b0b": "var(--secondary-background-color)", "#0b0f14": "var(--secondary-background-color)",
        "#111827": "var(--secondary-background-color)", "#0b0f14": "var(--secondary-background-color)",
        "#e5e7eb": "var(--text-color)", "#f3f4f6": "var(--text-color)",
        "#d1d5db": "var(--text-color)", "#c9d1d9": "var(--text-color)",
        "#aab2c0": "var(--text-color)", "#8b949e": "var(--text-color)",
        "#9ca3af": "var(--text-color)", "#f0c65a": "var(--primary-color)",
        "rgba(8,8,8,.96)": "var(--secondary-background-color)",
        "rgba(11,15,20,.95)": "var(--secondary-background-color)",
        "rgba(11,15,20,.92)": "var(--secondary-background-color)",
        "rgba(8,8,8,.76)": "var(--secondary-background-color)",
        "rgba(255,255,255,.08)": "color-mix(in srgb, var(--text-color) 18%, transparent)",
    }
    for old, new in mappings.items():
        if new.startswith("color-mix"):
            new = "color-mix(in srgb, currentColor 18%, transparent)"
        elif "background-color" in new:
            new = "color-mix(in srgb, currentColor 3%, transparent)"
        elif "--text-color" in new or "--primary-color" in new:
            new = "inherit"
        css = css.replace(old, new)
    css = re.sub(r'rgba\(255,\s*255,\s*255,\s*\.\d+\)', 'color-mix(in srgb, currentColor 16%, transparent)', css)
    return css


def inject_platform_css() -> None:
    """Apply the small set of global rules shared by the active product pages."""
    st.markdown(
        theme_css(
            """
            <style>
            .block-container { padding-top: 4.5rem; max-width: 1720px; }
            button p, button span { pointer-events: none; }
            [data-testid="stBaseButton-primary"],
            [data-testid="stBaseButton-primary"] p,
            [data-testid="stBaseButton-primary"] span { color: #172033 !important; }
            </style>
            """
        ),
        unsafe_allow_html=True,
    )


def render_appearance():
    """Fixed user control. The versioned DOM bridge invokes native theme actions.

    No config.toml writes or global server theme mutation. A failed bridge
    shows an explicit fallback message instead of painting only part of the UI.
    """
    st.iframe(Path(__file__).with_name("theme_switcher.html"), height=1, tab_index=-1, alt="悬浮外观控制初始化")
    st.session_state["chart_render_counter"] = 0


def render_chart(figure, **kwargs):
    """One card, one zoom control. Only one chart can own wheel zoom at a time."""
    import hashlib
    counter = st.session_state.get("chart_render_counter", 0)
    st.session_state.chart_render_counter = counter + 1
    key = kwargs.get("key") or f"legacy_chart_{counter}"
    token = hashlib.sha256(key.encode()).hexdigest()[:12]
    figure = go.Figure(figure)
    figure.update_layout(template=None, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         font_color=None, hoverlabel=dict(bgcolor=None, font_color=None))
    figure.update_xaxes(gridcolor="rgba(128,128,128,.18)", color=None)
    figure.update_yaxes(gridcolor="rgba(128,128,128,.18)", color=None)
    figure.update_annotations(font_color=None)
    for shape in figure.layout.shapes:
        if str(shape.line.color).startswith("rgba(255,255,255"):
            shape.line.color="rgba(128,128,128,.4)"
    with st.container(border=True, key=f"chart_card_{token}"):
        a,b=st.columns([3,1])
        active=st.session_state.get("active_chart_zoom")==key
        if a.button("关闭本图缩放" if active else "开启本图缩放",key=f"zoom_{key}",
                    help="仅控制这一张图。关闭时滚轮用于页面；开启后滚轮/触控板双指滚动可能缩放图表。"):
            st.session_state.active_chart_zoom = None if active else key
            st.rerun()
        if b.button("重置视图",key=f"reset_{key}"):
            st.session_state[f"revision_{key}"] = st.session_state.get(f"revision_{key}",0)+1
        active=st.session_state.get("active_chart_zoom")==key
        st.caption("本图缩放已开启；可拖动、框选及滚轮缩放。点击关闭后恢复页面滚动。" if active else "滚轮用于页面滚动；如需缩放，点击本图按钮。悬停仍可查看数据。")
        if key == "visualization_price_chart":
            st.markdown("#### 行情价格与成交量")
            st.caption("价格和成交量使用相同日期轴；缺失量额留空，×仅标识缺失，数值不是0。")
        figure.update_layout(uirevision=f"{figure.layout.uirevision}:{st.session_state.get(f'revision_{key}',0)}")
        config = {**kwargs.pop("config", {}), "displaylogo": False, "scrollZoom": active}
        kwargs.setdefault("theme", "streamlit")
        st.plotly_chart(figure, config=config, **kwargs)


@st.fragment(run_every="30s")
def temporary_heartbeat():
    """Fragment timers run while the session is active; no market requests."""
    sid = st.session_state.get("temporary_cache_id")
    for value in st.session_state.get("temporary_cache_roots", []):
        root = Path(value)
        start_cleaner(root)
        if sid:
            try:
                touch(root, sid)
            except (OSError, ValueError):
                st.caption("临时会话已过期或暂时无法续期，请重新获取数据或在数据管理中检查。")
