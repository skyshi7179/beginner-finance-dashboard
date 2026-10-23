"""Streamlit presentation for the platform home and honest feature placeholders."""

from __future__ import annotations

from html import escape

import streamlit as st

from src.navigation import (
    PAGE_AI_REPORT,
    PAGE_BACKTEST,
    PAGE_CONFIG,
    PAGE_DATA_ACQUISITION,
    PAGE_DATA_MANAGEMENT,
    PAGE_VISUALIZATION,
)


CORE_FEATURES = (
    (
        PAGE_DATA_ACQUISITION,
        "📥",
        "股票数据获取",
        "输入代码或名称，搜索 A 股股票或场内 ETF，获取历史行情并保存独立本地缓存；支持日期区间、状态查看和 CSV 导出。",
        "进入数据获取",
    ),
    (
        PAGE_VISUALIZATION,
        "📈",
        "金融数据可视化",
        "查看股票 K 线、成交量、MA5/MA20 均线、日收益率、累计收益率和交互式金融图表。",
        "进入可视化分析",
    ),
    (
        PAGE_BACKTEST,
        "🧪",
        "自定义策略回测",
        "从均线、通道、RSI、日线研究网格或自定义条件开始，与买入持有基准比较历史结果。",
        "进入策略回测",
    ),
)

ADVANCED_FEATURES = (
    (PAGE_AI_REPORT, "研究报告", "基于股票历史行情、技术指标及策略回测结果，生成结构化金融研究报告；无API时使用明确标识的规则模板。"),
    (PAGE_DATA_MANAGEMENT, "数据管理", "管理永久与本次临时行情，批量回收或恢复缓存，并配置研究报告与策略助手使用的模型 API。"),
)


def render_home_page() -> str | None:
    """Render the lightweight home screen and return a requested route."""
    st.markdown(
        """
        <style>
        .home-feature-description {
            min-height: 4.8em;
            line-height: 1.55;
            color: inherit;
        }
        .home-advanced-description {
            min-height: 4.2em;
            line-height: 1.55;
            color: inherit;
        }
        @media (max-width: 768px) {
            .home-feature-description,
            .home-advanced-description { min-height: 0; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("面向新手金融数据分析平台")
    st.write("从获取行情、看懂图表到策略回测，逐步完成你的金融数据分析。")
    st.caption("第一次使用：获取数据 → 查看图表 → 运行回测 → 生成报告。")
    st.markdown("### 核心功能")

    columns = st.columns(3, gap="large")
    selected_page = None
    for column, (page, icon, title, description, button_label) in zip(columns, CORE_FEATURES):
        with column:
            with st.container(border=True):
                st.markdown(f"### {icon} {title}")
                st.caption(f"当前阶段：{PAGE_CONFIG[page]['status']}。")
                st.markdown(
                    f'<div class="home-feature-description">{escape(description)}</div>',
                    unsafe_allow_html=True,
                )
                if st.button(button_label, key=f"home_enter_{page}", type="primary", use_container_width=True):
                    selected_page = page

    st.markdown("### 进阶分析")
    advanced_columns = st.columns(len(ADVANCED_FEATURES), gap="large")
    for column, (page, title, description) in zip(advanced_columns, ADVANCED_FEATURES):
        with column:
            with st.container(border=True):
                st.markdown(f"#### {title}")
                st.markdown(
                    f'<div class="home-advanced-description">{escape(description)}</div>',
                    unsafe_allow_html=True,
                )
                if page == PAGE_AI_REPORT:
                    button_label = "进入研究报告"
                    button_type = "secondary"
                else:
                    button_label = "进入数据管理"
                    button_type = "secondary"
                if st.button(button_label, key=f"home_enter_{page}", type=button_type, use_container_width=True):
                    selected_page = page

    st.caption("行情与研究结果会标明来源；本平台不提供交易建议或未来收益承诺。")
    return selected_page


def render_placeholder_page(page: str) -> bool:
    """Render an unimplemented feature page. Return True when home is requested."""
    config = PAGE_CONFIG[page]
    return_clicked = st.button("← 返回首页", key=f"return_home_{page}")
    st.title(config["title"])
    st.info(f"功能状态：{config['status']}")
    st.write(config["description"])
    return return_clicked
