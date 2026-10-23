"""Offline acceptance for calendar, API settings and cache recovery UX."""

import json
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from src.stock_data.calendar import session_gap
from src.stock_data.management import archive_entry, list_recovery_entries
from src.stock_data.turnover import turnover_issue_summary, turnover_metadata
from src.stock_data.units import turnover_gaps
from test_backtest_page import seed_cache


APP = Path(__file__).resolve().parents[1] / "app.py"


def test_calendar_distinguishes_holiday_missing_session_and_unknown_coverage():
    assert session_gap("2024-09-30", "2024-10-08").status == "CONTIGUOUS"
    missing = session_gap("2024-01-02", "2024-01-04")
    assert missing.status == "MISSING_SESSION" and "2024-01-03" in missing.missing_sessions
    assert session_gap("2027-01-04", "2027-01-05").status == "UNKNOWN_CALENDAR"


def test_turnover_issue_is_not_described_as_missing_trading_days():
    frame = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03"], "volume": [1000, 1200], "amount": [None, None],
        "volume_unit": ["份", "份"], "amount_unit": ["元", "元"],
        "volume_source": ["Tencent", "Tencent"], "amount_source": ["", ""],
        "turnover_status": ["CONFLICT", "PARTIAL"],
        "turnover_message": ["两源成交量不同", "成交额暂缺"],
    })
    metadata = turnover_metadata(frame)
    text = turnover_issue_summary(metadata)
    assert "来源冲突1条" in text and "字段待补充1条" in text
    assert "不表示缺少交易日" in text and metadata["turnover_field_missing_rows"] == 2
    issues = turnover_gaps(frame)
    assert issues["价格日线"].eq("存在").all()
    assert issues["问题分类"].tolist() == ["来源冲突", "字段待补充"]


def test_recovery_catalog_uses_security_details_and_records_archive_time(tmp_path):
    directory = tmp_path / "stocks"
    seed_cache(directory)
    target = archive_entry(directory, {
        "symbol": "600519", "name": "离线测试证券", "market": "上海",
        "asset_type": "A股", "adjustment": "qfq", "保存方式": "永久",
    }, tmp_path / "recovery")
    entries, errors = list_recovery_entries(tmp_path / "recovery")
    assert not errors and len(entries) == 1
    item = entries[0]
    assert item["symbol"] == "600519" and item["name"] == "离线测试证券"
    assert item["record_count"] == 90 and item["start"] <= item["end"]
    assert item["archived_at"] != "旧条目未记录"
    assert target.name not in " ".join(str(value) for value in item.values() if value != item["manifest"])


def test_model_profile_is_session_only_and_never_calls_on_save(tmp_path, monkeypatch):
    import src.ai_report.settings as settings
    import src.stock_data.management as management

    monkeypatch.setattr(management, "DATA_DIR", tmp_path / "stocks")
    calls = []
    monkeypatch.setattr(settings, "call_model", lambda config, messages: calls.append((config, messages)) or '{"ok":true}')
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_data_management").click().run()
    assert calls == []
    app.text_input(key="model_api_key").set_value("test-session-key").run()
    assert calls == []
    app.button(key="model_config_save").click().run()
    assert calls == [] and app.session_state[settings.RUNTIME_MODEL_KEY]["model"] == "glm-4.7-flash"
    from src.ai_report.models import configured_models
    models, errors = configured_models(environ={}, secrets={}, runtime=app.session_state[settings.RUNTIME_MODEL_KEY])
    assert not errors and "test-session-key" not in repr(models[0])
    app.button(key="model_config_test").click().run()
    assert len(calls) == 1 and not app.exception


def test_custom_strategy_hides_generic_strategy_guide(tmp_path, monkeypatch):
    import src.backtest.page as page

    seed_cache(tmp_path)
    monkeypatch.setattr(page, "DATA_DIR", tmp_path)
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    assert any(exp.label == "这项策略在做什么？" for exp in app.expander)
    app.selectbox(key="strategy_kind").select("custom").run()
    assert all(exp.label != "这项策略在做什么？" for exp in app.expander)
    assert not app.exception
