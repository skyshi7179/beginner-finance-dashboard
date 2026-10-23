"""Generate, validate, render; any rejected model content stays out of reports."""

from dataclasses import dataclass
import re
from threading import Lock

from src.ai_report.data import AnalysisPacket, REPORT_TYPES, SECTION_TITLES
import time

from src.ai_report.models import ModelCallError, ModelConfig, call_model
from src.ai_report.prompts import PROMPT_VERSION, build_messages
from src.ai_report.validation import ReportValidationError, make_rule_plan, parse_json, validate_plan
from src.time_utils import get_china_now


_GENERATION_LOCK = Lock()


class ReportBusyError(RuntimeError):
    pass


@dataclass
class GeneratedReport:
    markdown: str
    metrics_csv: bytes
    payload: dict
    plan: dict
    mode: str
    validation: str
    fallback_reason: str | None


def _escape(value) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", str(value).replace("\n", " ").replace("\r", " "))


def generate_report(packet: AnalysisPacket, model: ModelConfig | None = None, *, model_call=None) -> GeneratedReport:
    if not _GENERATION_LOCK.acquire(blocking=False):
        raise ReportBusyError("当前已有报告正在生成，请等待完成后再试。")
    try:
        return _generate(packet, model, model_call or _call_model_with_overload_retry)
    finally:
        _GENERATION_LOCK.release()


def _call_model_with_overload_retry(model, messages):
    for attempt, delay in enumerate((2.0, 5.0, None)):
        try:
            return call_model(model, messages)
        except ModelCallError as exc:
            if exc.business_code != "1305" or delay is None:
                raise
            time.sleep(delay)


def _generate(packet, model, model_call):
    payload = packet.payload
    reason = None
    mode = "LOCAL_RULES"
    plan = make_rule_plan(payload)
    if model is not None:
        try:
            content = model_call(model, build_messages(payload))
            plan = validate_plan(parse_json(content), payload)
            mode = "LLM_VERIFIED_PLAN"
        except (ModelCallError, ReportValidationError) as exc:
            reason = str(exc)
            mode = "RULES_FALLBACK"
            plan = make_rule_plan(payload)
    validate_plan(plan, payload)  # Rule output is checked by the same local validator.
    validation = ("模型结构、证券身份、日期、适用句式与全部引用值已通过本地核验；模型仅编排受控句式。"
                  if mode == "LLM_VERIFIED_PLAN" else "本地规则报告；未将模型自由文本标记为已通过核验。")
    mode_label = {"LOCAL_RULES": "本地规则报告（未调用LLM）", "RULES_FALLBACK": "本地规则报告（模型失败或核验拒绝后回退）",
                  "LLM_VERIFIED_PLAN": "LLM辅助报告（受控句式编排）"}[mode]
    lines = ["# 研究报告", "", f"报告类型：{REPORT_TYPES[payload['report_type']]}", "",
             f"生成方式：{mode_label}", "", f"生成时刻：{get_china_now().isoformat()}", "",
             f"核验范围：{validation}", "", "核验不等于独立认证上游数据真实性，也不证明策略样本外有效。", ""]
    if reason:
        lines.extend([f"回退原因：{_escape(reason)}", ""])
    for section, title in SECTION_TITLES.items():
        lines.extend([f"## {section}. {title}", ""])
        for key in plan["sections"][section]:
            statement = payload["statements"][key]
            rendered = re.sub(r"\{\{([\w.]+)\}\}", lambda match: _escape(payload["facts"][match.group(1)]["display"]), statement["template"])
            lines.extend([rendered, ""])
    if payload["warnings"]:
        lines.extend(["### 数据充分性与计算提示", ""])
        lines.extend(f"- {_escape(warning)}" for warning in payload["warnings"])
        lines.append("")
    lines.extend(["### 复现与引用核对", "", f"- 分析标识：`{payload['analysis_id']}`",
                  f"- 缓存内容摘要：`{payload['data_digest']}`", f"- Prompt版本：`{PROMPT_VERSION}`",
                  f"- 模型：{_escape(model.model) if model else '未调用外部模型'}",
                  "- 精确值、显示值、单位与计算来源见随附指标CSV；百分比原始值以小数保存。", ""])
    return GeneratedReport("\n".join(lines), packet.metrics_csv, payload, plan, mode, validation, reason)
