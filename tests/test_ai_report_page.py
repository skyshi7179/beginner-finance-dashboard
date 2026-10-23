"""Offline AppTest: explicit generation, signatures, exports and navigation."""

from datetime import date
import json
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

import src.ai_report.page as page
import src.ai_report.service as report_service
from src.ai_report.validation import make_rule_plan
from src.stock_data.cache import save_history_cache
from test_ai_report import fixture_history, MODEL


APP = Path(__file__).resolve().parents[1] / "app.py"


def seed(directory, symbol="600519", name="离线测试证券", count=80):
    history = fixture_history(count)
    history.data["symbol"] = symbol
    history.data["name"] = name
    meta = {**history.metadata, "coverage_ranges":[{"start":"2024-01-01","end":"2024-06-30"}]}
    save_history_cache(history.data, meta, symbol, data_dir=directory)
    return history.data, meta


def open_page(directory, monkeypatch, *, remote=False, bad_response=False):
    calls = {"build":0, "model":0}
    original = page.build_analysis
    def build(*args, **kwargs):
        calls["build"] += 1
        return original(*args, **kwargs)
    def model(config, messages):
        calls["model"] += 1
        if bad_response:
            return '{"summary":"未来上涨999%"}'
        payload = json.loads(messages[1]["content"].split("\n",1)[1])
        return json.dumps(make_rule_plan(payload), ensure_ascii=False)
    def no_network(*args, **kwargs):
        pytest.fail("AI report UI must not download or make an unmocked API call")
    monkeypatch.setattr(page, "DATA_DIR", directory)
    monkeypatch.setattr(page, "load_models", lambda: ([MODEL] if remote else [], []))
    monkeypatch.setattr(page, "build_analysis", build)
    monkeypatch.setattr(report_service, "call_model", model)
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert app.button(key="home_enter_ai_report").label == "进入研究报告"
    app.button(key="home_enter_ai_report").click().run()
    return app, calls


def test_no_cache_has_real_return_paths_and_never_generates(tmp_path, monkeypatch):
    app, calls = open_page(tmp_path, monkeypatch)
    assert "研究报告" in [item.value for item in app.title]
    assert calls == {"build":0,"model":0} and not app.exception
    app.button(key="ai_report_no_cache").click().run()
    assert app.session_state["app_page"] == "data_acquisition"


def test_no_api_generates_labeled_rule_report_and_downloads(tmp_path, monkeypatch):
    seed(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch)
    assert calls == {"build":0,"model":0}
    assert app.selectbox(key="ai_report_model").options == ["本地规则模板（无需API）"]
    assert app.button(key="ai_report_regenerate").disabled
    app.button(key="ai_report_generate").click().run()
    assert not app.exception
    result = app.session_state["ai_report_result"]
    assert result.mode == "LOCAL_RULES" and "未调用LLM" in result.markdown
    assert calls == {"build":1,"model":0}
    assert len(app.get("download_button")) == 2
    assert not app.button(key="ai_report_regenerate").disabled


def test_remote_calls_only_on_generate_or_explicit_regenerate(tmp_path, monkeypatch):
    seed(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch, remote=True)
    app.run()
    assert calls == {"build":0,"model":0}
    app.button(key="ai_report_generate").click().run()
    assert calls == {"build":1,"model":1}
    assert app.session_state["ai_report_result"].mode == "LLM_VERIFIED_PLAN"
    app.run()
    app.button(key="ai_report_generate").click().run()
    assert calls == {"build":1,"model":1}
    app.button(key="ai_report_regenerate").click().run()
    assert calls == {"build":2,"model":2} and not app.exception


@pytest.mark.parametrize("change", ["type", "date", "window", "stock", "adjustment", "model", "cache"])
def test_changed_context_hides_old_report_without_automatic_model_call(tmp_path, monkeypatch, change):
    frame, meta = seed(tmp_path)
    save_history_cache(frame.assign(adjustment="hfq"), meta, "600519", adjustment="hfq", data_dir=tmp_path)
    app, calls = open_page(tmp_path, monkeypatch, remote=True)
    app.button(key="ai_report_generate").click().run()
    assert len(app.get("download_button")) == 2
    if change == "type": app.selectbox(key="ai_report_type").select("technical").run()
    elif change == "date": app.date_input(key="ai_report_dates").set_value((date(2024,1,5),date(2024,6,30))).run()
    elif change == "window": app.number_input(key="ai_report_short").set_value(6).run()
    elif change == "stock":
        seed(tmp_path, "000001", "另一个测试证券")
        app.run()
        app.selectbox(key="ai_report_security").select("A股:000001").run()
    elif change == "adjustment": app.selectbox(key="ai_report_adjustment").select("hfq").run()
    elif change == "model": app.selectbox(key="ai_report_model").select(page.LOCAL_MODEL).run()
    elif change == "cache":
        frame.loc[0,"close"] += .5
        save_history_cache(frame, meta, "600519", data_dir=tmp_path)
        app.run()
    assert calls == {"build":1,"model":1}
    assert len(app.get("download_button")) == 0 and app.button(key="ai_report_regenerate").disabled
    assert not app.exception


def test_return_home_retains_context_report_and_does_not_regenerate(tmp_path, monkeypatch):
    seed(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch, remote=True)
    app.selectbox(key="ai_report_type").select("technical").run()
    app.button(key="ai_report_generate").click().run()
    app.button(key="return_home_ai_report").click().run()
    assert app.session_state["app_page"] == "home"
    app.button(key="home_enter_ai_report").click().run()
    assert app.selectbox(key="ai_report_type").value == "technical"
    assert len(app.get("download_button")) == 2
    assert calls == {"build":1,"model":1} and not app.exception


def test_model_invalid_output_falls_back_and_never_renders_rejected_text(tmp_path, monkeypatch):
    seed(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch, remote=True, bad_response=True)
    app.button(key="ai_report_generate").click().run()
    result = app.session_state["ai_report_result"]
    assert result.mode == "RULES_FALLBACK"
    assert "未来上涨999%" not in result.markdown
    assert len(app.get("download_button")) == 2 and not app.exception
    assert calls["model"] == 1


def test_local_search_has_explicit_selection_and_missing_search_links_acquisition(tmp_path, monkeypatch):
    seed(tmp_path)
    seed(tmp_path, "000001", "另一个测试证券")
    app, calls = open_page(tmp_path, monkeypatch)
    app.text_input(key="ai_report_search").set_value("测试证券").run()
    assert len(app.selectbox(key="ai_report_security").options) == 2
    app.text_input(key="ai_report_search").set_value("600519").run()
    assert app.selectbox(key="ai_report_security").value == "A股:600519"
    app.text_input(key="ai_report_search").set_value("不存在的名称").run()
    assert app.button(key="ai_report_search_missing")
    assert calls == {"build":0,"model":0}


def test_short_series_explains_missing_strategy_and_invalid_parameters_block_api(tmp_path, monkeypatch):
    seed(tmp_path, count=6)
    app, calls = open_page(tmp_path, monkeypatch, remote=True)
    app.button(key="ai_report_generate").click().run()
    assert app.session_state["ai_report_result"].payload["backtest_state"] == "insufficient"
    assert "strategy.cumulative_return" not in app.session_state["ai_report_result"].payload["facts"]
    app.number_input(key="ai_report_short").set_value(30).run()
    app.button(key="ai_report_generate").click().run()
    assert any("短期窗口" in error.value for error in app.error)
    assert calls["model"] == 1 and len(app.get("download_button")) == 0
    assert not app.exception


def test_empty_selected_interval_never_generates(tmp_path, monkeypatch):
    seed(tmp_path)
    app, calls = open_page(tmp_path, monkeypatch, remote=True)
    app.date_input(key="ai_report_dates").set_value((date(2023,1,1),date(2023,2,1))).run()
    assert app.button(key="ai_report_empty_range")
    assert calls == {"build":0,"model":0} and not app.exception
