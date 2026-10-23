"""Small, session-state-backed navigation helpers for the Streamlit app."""

from __future__ import annotations

from typing import MutableMapping


PAGE_HOME = "home"
PAGE_DATA_ACQUISITION = "data_acquisition"
PAGE_VISUALIZATION = "visualization"
PAGE_BACKTEST = "backtest"
PAGE_AI_REPORT = "ai_report"
PAGE_DATA_MANAGEMENT = "data_management"

PAGE_CONFIG = {
    PAGE_HOME: {
        "title": "面向新手金融数据分析平台",
        "status": "首页",
    },
    PAGE_DATA_ACQUISITION: {
        "title": "股票 / ETF 数据获取",
        "status": "MVP 已实现",
        "description": "支持 A 股与场内 ETF 代码和名称搜索、对应日线行情获取、独立本地缓存及 CSV 导出。数据请求只由用户按钮触发。",
    },
    PAGE_VISUALIZATION: {
        "title": "金融数据可视化",
        "status": "MVP 已实现",
        "description": "只读本地缓存，展示 K 线、成交量、MA5/MA20、收益率及回撤；支持指标 CSV 下载。",
    },
    PAGE_BACKTEST: {
        "title": "自定义策略回测",
        "status": "研究回测 MVP 已实现",
        "description": "使用本地缓存进行双均线归一化研究回测：收盘交叉信号、下一开盘执行、模拟成本与买入持有基准。",
    },
    PAGE_AI_REPORT: {
        "title": "研究报告",
        "status": "研究报告MVP已实现",
        "description": "复用缓存、指标与回测生成可核验研究报告，支持已配置模型及无API规则模板。",
    },
    PAGE_DATA_MANAGEMENT: {
        "title": "数据管理",
        "status": "缓存管理已实现",
        "description": "查看永久/会话临时行情、转存、清理与回收恢复。",
    },
}


def get_current_page(session_state: MutableMapping) -> str:
    """Return a valid current page, defaulting safely to the home page."""
    page = session_state.get("app_page", PAGE_HOME)
    return page if page in PAGE_CONFIG else PAGE_HOME


def navigate_to(session_state: MutableMapping, page: str) -> None:
    """Store a valid route in Streamlit session state."""
    session_state["app_page"] = page if page in PAGE_CONFIG else PAGE_HOME
