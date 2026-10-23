"""Offline financial fact, prompt, output-verification and transport tests."""

from copy import deepcopy
from datetime import date
from io import BytesIO
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import requests

import src.ai_report.data as data_module
import src.ai_report.service as service
from src.ai_report.data import REPORT_TYPES, ReportInputError, build_analysis
from src.ai_report.models import ModelCallError, ModelConfig, call_model, configured_models
from src.ai_report.prompts import SYSTEM_PROMPT, build_messages
from src.ai_report.validation import ReportValidationError, make_rule_plan, parse_json, validate_plan
from src.backtest.engine import BacktestParams, run_backtest
from src.stock_analysis.indicators import calculate_indicators
from src.stock_data.provider import fetch_stock_history
from src.stock_data.service import StockHistoryResult


SELECTED = {"symbol": "600519", "name": "离线测试证券", "asset_type": "A股"}
MODEL = ModelConfig("configured-test-model", "https://example.invalid/v1", "dummy-secret-never-export")


def fixture_history(count=80):
    close = 100 + np.sin(np.arange(count) / 3) * 12 + np.arange(count) / 10
    raw = pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=count).strftime("%Y-%m-%d"),
                        "open": close + .2, "high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    frame = fetch_stock_history("600519", raw.date.min(), raw.date.max(), name=SELECTED["name"], market="上海", fetcher=lambda **kwargs: raw)
    return StockHistoryResult(frame, "CACHE", {"cache_updated_at": "2024-06-01T10:00:00+08:00"})


def packet_for(history=None, report_type="comprehensive", params=None):
    history = history if history is not None else fixture_history()
    return build_analysis(history, SELECTED, start="2024-01-01", end="2024-06-30", adjustment="qfq", report_type=report_type, params=params)


@pytest.mark.parametrize("report_type", list(REPORT_TYPES))
def test_all_report_types_have_complete_sections_finite_json_and_local_report(report_type):
    packet = packet_for(report_type=report_type)
    json.dumps(packet.payload, allow_nan=False)
    report = service.generate_report(packet)
    assert report.mode == "LOCAL_RULES" and "未调用LLM" in report.markdown
    assert all(f"## {section}." in report.markdown for section in "ABCDE")
    assert validate_plan(report.plan, packet.payload) == report.plan


def test_metrics_reuse_existing_calculations_without_changing_input():
    history = fixture_history()
    before = history.data.copy(deep=True)
    packet = packet_for(history)
    facts = packet.payload["facts"]
    indicators = calculate_indicators(history.data)
    engine = run_backtest(history.data)
    assert facts["history.cumulative_return"]["value"] == indicators.metrics["cumulative_return"]
    assert facts["history.max_drawdown"]["value"] == indicators.metrics["max_drawdown"]
    assert facts["technical.ma20"]["value"] == indicators.data.ma20.iloc[-1]
    assert facts["strategy.cumulative_return"]["value"] == engine.metrics.loc["双均线策略", "cumulative_return"]
    assert facts["backtest.return_difference"]["value"] == pytest.approx((engine.metrics.loc["双均线策略", "cumulative_return"]-engine.metrics.loc["买入持有", "cumulative_return"])*100)
    pd.testing.assert_frame_equal(history.data, before)


def test_non_strategy_report_does_not_run_engine(monkeypatch):
    monkeypatch.setattr(data_module, "run_backtest", lambda *args, **kwargs: pytest.fail("must not run unrequested backtest"))
    assert packet_for(report_type="technical").payload["backtest_state"] == "not_requested"


def test_short_series_retains_null_and_discloses_missing_backtest():
    packet = packet_for(fixture_history(6))
    assert packet.payload["facts"]["technical.ma20"]["value"] is None
    assert packet.payload["backtest_state"] == "insufficient"
    assert "strategy.cumulative_return" not in packet.payload["facts"]
    report = service.generate_report(packet)
    assert "未生成有效回测" in report.markdown and "数据不足（未计算）" in report.markdown


def test_single_bar_and_partial_cache_are_disclosed_without_inventing_dates():
    history = fixture_history(1)
    history.status = "STALE_CACHE"
    packet = packet_for(history)
    assert packet.payload["period"] == {"start":"2024-01-02", "end":"2024-01-02"}
    assert packet.payload["facts"]["history.annualized_volatility"]["value"] is None
    assert any("未完全覆盖" in warning for warning in packet.payload["warnings"])


@pytest.mark.parametrize("problem", ["empty", "symbol", "name", "source", "source_type", "adjustment", "sample", "demo_provider", "missing_date", "date", "bad_price", "bad_window", "unknown_type"])
def test_invalid_inputs_fail_before_any_model_request(problem):
    history = fixture_history()
    params, report_type = None, "comprehensive"
    if problem == "empty": history.data = history.data.iloc[:0]
    elif problem == "symbol": history.data.loc[0,"symbol"] = "000001"
    elif problem == "name": history.data["name"] = "另一个名称"
    elif problem == "source": history.data.loc[0,"source"] = "OtherProvider"
    elif problem == "source_type": history.data["source"] = 42
    elif problem == "adjustment": history.data.loc[0,"adjustment"] = "hfq"
    elif problem == "sample": history.data["source"] = "SAMPLE"
    elif problem == "demo_provider": history.data["provider"] = "DEMO"
    elif problem == "missing_date": history.data = history.data.drop(columns="date")
    elif problem == "date": history.data.loc[0,"date"] = "2030-01-01"
    elif problem == "bad_price": history.data.loc[0,"close"] = -1
    elif problem == "bad_window": params = BacktestParams(short_window=30,long_window=20)
    elif problem == "unknown_type": report_type = "invented"
    with pytest.raises(ReportInputError):
        packet_for(history, report_type, params)


def test_valid_mock_model_can_reorder_statements_and_pass_citation_verification():
    packet = packet_for()
    plan = make_rule_plan(packet.payload)
    plan["sections"]["B"].reverse()
    calls = []
    def model_call(config, messages):
        calls.append(messages)
        return json.dumps(plan, ensure_ascii=False)
    report = service.generate_report(packet, MODEL, model_call=model_call)
    assert report.mode == "LLM_VERIFIED_PLAN" and len(calls) == 1
    assert "受控句式" in report.markdown and "核验不等于独立认证" in report.markdown
    assert MODEL.api_key not in report.markdown and MODEL.api_key not in str(calls)


@pytest.mark.parametrize("attack", ["number", "name", "symbol", "date", "analysis_id", "free_text", "missing_section", "missing_limits", "unknown_statement", "wrong_section", "missing_citation", "extra_citation", "duplicate_statement", "boolean", "rounding", "huge_number"])
def test_tampered_model_plan_is_rejected_and_falls_back(attack):
    packet = packet_for()
    plan = make_rule_plan(packet.payload)
    if attack == "number": plan["citations"]["history.cumulative_return"] = .999
    elif attack == "name": plan["instrument"]["name"] = "虚构名称"
    elif attack == "symbol": plan["instrument"]["symbol"] = "000001"
    elif attack == "date": plan["period"]["end"] = "2099-01-01"
    elif attack == "analysis_id": plan["analysis_id"] = "wrong"
    elif attack == "free_text": plan["summary"] = "预测上涨999%，立即买入"
    elif attack == "missing_section": del plan["sections"]["E"]
    elif attack == "missing_limits": plan["sections"]["E"] = ["research_units"]
    elif attack == "unknown_statement": plan["sections"]["B"].append("未来上涨999%")
    elif attack == "wrong_section": plan["sections"]["A"].append("performance")
    elif attack == "missing_citation": del plan["citations"]["instrument.symbol"]
    elif attack == "extra_citation": plan["citations"]["unverified"] = 10
    elif attack == "duplicate_statement": plan["sections"]["A"].append("overview")
    elif attack == "boolean": plan["citations"]["backtest.risk_free_rate"] = False
    elif attack == "rounding": plan["citations"]["history.cumulative_return"] = round(plan["citations"]["history.cumulative_return"], 2)
    elif attack == "huge_number": plan["citations"]["history.cumulative_return"] = 10**1000
    report = service.generate_report(packet, MODEL, model_call=lambda *args: json.dumps(plan, ensure_ascii=False))
    assert report.mode == "RULES_FALLBACK" and report.fallback_reason
    assert "预测上涨999%" not in report.markdown
    assert report.plan == make_rule_plan(packet.payload)


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', '[]', '```json\n{}\n```', '{"x":', '{"x":Infinity}'])
def test_non_strict_json_is_rejected(content):
    with pytest.raises(ReportValidationError):
        parse_json(content)


def test_unavailable_value_cannot_be_replaced_with_zero():
    packet = packet_for(fixture_history(6))
    plan = make_rule_plan(packet.payload)
    plan["citations"]["technical.ma20"] = 0
    with pytest.raises(ReportValidationError):
        validate_plan(plan, packet.payload)


def test_prompt_is_explicit_about_input_output_rules_and_no_secrets():
    messages = build_messages(packet_for().payload)
    assert {item["role"] for item in messages} == {"system", "user"}
    for text in ("JSON", "required", "citations", "不能计算", "数据", "回退", "自由文本"):
        assert text in SYSTEM_PROMPT
    assert MODEL.api_key not in str(messages)


def test_exact_numbers_alone_do_not_validate_a_free_text_direction_claim():
    packet = packet_for()
    plan = make_rule_plan(packet.payload)
    # The numeric citations are all exact, but an unrestricted explanation can
    # still contradict them. Extra narrative is rejected instead of certified.
    plan["explanation"] = "无论历史数据如何，该证券今后必然持续上涨。"
    assert all(value == packet.payload["facts"][key]["value"] for key, value in plan["citations"].items())
    with pytest.raises(ReportValidationError):
        validate_plan(plan, packet.payload)


def test_csv_has_python_values_source_and_reproduction_metadata():
    packet = packet_for()
    frame = pd.read_csv(BytesIO(packet.metrics_csv))
    assert "value（原始值）" in frame and "origin（计算来源）" in frame
    cell = frame[frame["fact_id（指标标识）"].eq("history.cumulative_return")].iloc[0]
    assert float(cell["value（原始值）"]) == pytest.approx(packet.payload["facts"]["history.cumulative_return"]["value"])
    assert frame.analysis_id.eq(packet.payload["analysis_id"]).all()


def test_markdown_escapes_untrusted_stock_name():
    history = fixture_history()
    history.data["name"] = "[伪链接](https://example.invalid)"
    selected = {**SELECTED, "name": history.data.name.iloc[0]}
    packet = build_analysis(history, selected, start="2024-01-01", end="2024-06-30", adjustment="qfq", report_type="history")
    report = service.generate_report(packet)
    assert "\\[伪链接\\]" in report.markdown


def test_model_config_only_exposes_complete_explicit_configurations():
    assert configured_models(environ={}) == ([], [])
    assert not configured_models(environ={"RESEARCH_LLM_API_KEY":"secret"})[0]
    env = {"RESEARCH_LLM_MODEL":"custom", "RESEARCH_LLM_API_KEY":"secret", "RESEARCH_LLM_BASE_URL":"https://example.invalid/v1"}
    models, errors = configured_models(environ=env)
    assert len(models) == 1 and not errors and "secret" not in repr(models[0])
    secret_models, _ = configured_models(environ={}, secrets=env)
    assert secret_models == models
    for address in ("http://remote.invalid/v1", "https://user:pass@example.invalid/v1", "https://example.invalid/v1?key=secret", "ftp://example.invalid", "https://[bad", "https:// bad.invalid"):
        assert not configured_models(environ={**env,"RESEARCH_LLM_BASE_URL":address})[0]


def test_future_cache_observations_cannot_be_presented_as_history():
    history = fixture_history()
    history.data["date"] = "2099-01-01"
    with pytest.raises(ReportInputError, match="未来日期"):
        build_analysis(history, SELECTED, start="2099-01-01", end="2099-12-31", adjustment="qfq", report_type="history")


class FakeResponse:
    def __init__(self, envelope=None, status=200):
        self.status_code = status
        self.envelope = envelope if envelope is not None else {"choices":[{"finish_reason":"stop", "message":{"content":"{}"}}]}
        self.content = json.dumps(self.envelope).encode()
        self.closed = False
    def json(self): return self.envelope
    def close(self): self.closed = True


def test_transport_uses_single_explicit_post_with_timeout_and_no_redirect(monkeypatch):
    calls = []
    response = FakeResponse()
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response
    monkeypatch.setattr(requests, "post", post)
    assert call_model(MODEL, build_messages(packet_for().payload)) == "{}"
    assert len(calls) == 1 and calls[0][0].endswith("/v1/chat/completions")
    args = calls[0][1]
    assert args["timeout"] == (5,25) and not args["allow_redirects"]
    assert args["json"]["response_format"] == {"type":"json_object"}
    assert args["headers"]["Authorization"] == f"Bearer {MODEL.api_key}" and response.closed


def test_configured_transport_to_verified_report_end_to_end_with_mock_http(monkeypatch):
    packet = packet_for()
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        payload = json.loads(kwargs["json"]["messages"][1]["content"].split("\n",1)[1])
        content = json.dumps(make_rule_plan(payload), ensure_ascii=False)
        return FakeResponse({"choices":[{"finish_reason":"stop", "message":{"content":content}}]})
    monkeypatch.setattr(requests, "post", post)
    result = service.generate_report(packet, MODEL)
    assert len(calls) == 1 and result.mode == "LLM_VERIFIED_PLAN"
    assert MODEL.api_key not in result.markdown and MODEL.api_key.encode() not in result.metrics_csv


@pytest.mark.parametrize("problem", ["timeout", "network", "http", "redirect", "truncated", "refusal", "bad_envelope", "empty", "large"])
def test_api_failure_returns_rule_report_without_leaking_secret(monkeypatch, problem):
    def post(*args, **kwargs):
        if problem == "timeout": raise requests.Timeout(MODEL.api_key)
        if problem == "network": raise requests.ConnectionError(MODEL.api_key)
        if problem == "http": return FakeResponse(status=401)
        if problem == "redirect": return FakeResponse(status=302)
        if problem == "truncated": return FakeResponse({"choices":[{"finish_reason":"length", "message":{"content":"{"}}]})
        if problem == "refusal": return FakeResponse({"choices":[{"finish_reason":"stop", "message":{"refusal":"no","content":""}}]})
        if problem == "bad_envelope": return FakeResponse({"choices":[None]})
        if problem == "empty": return FakeResponse({"choices":[{"finish_reason":"stop", "message":{"content":""}}]})
        response = FakeResponse(); response.content = b"x" * 200001
        return response
    monkeypatch.setattr(requests, "post", post)
    report = service.generate_report(packet_for(), MODEL)
    assert report.mode == "RULES_FALLBACK" and MODEL.api_key not in report.markdown


def test_concurrent_generation_is_rejected_without_model_call():
    with service._GENERATION_LOCK:
        with pytest.raises(service.ReportBusyError):
            service.generate_report(packet_for(), MODEL, model_call=lambda *args: pytest.fail("busy"))
