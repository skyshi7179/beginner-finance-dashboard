"""A strict output language: verified statement IDs and exact fact citations."""

from __future__ import annotations

import json
import math


class ReportValidationError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportValidationError("JSON包含重复键，已拒绝。")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ReportValidationError("JSON包含非有限数值，已拒绝。")


def parse_json(content: str) -> dict:
    if not isinstance(content, str) or len(content) > 100_000:
        raise ReportValidationError("模型输出不是有效文本或超过大小上限。")
    try:
        result = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ReportValidationError("模型输出不是严格JSON对象。") from exc
    if not isinstance(result, dict):
        raise ReportValidationError("模型输出必须是JSON对象。")
    return result


def make_rule_plan(payload: dict) -> dict:
    sections = {section: [key for key, item in payload["statements"].items() if item["section"] == section]
                for section in payload["required"]}
    refs = {ref for keys in sections.values() for key in keys for ref in payload["statements"][key]["refs"]}
    return {"schema_version": "1.0", "analysis_id": payload["analysis_id"],
            "instrument": dict(payload["instrument"]), "period": dict(payload["period"]),
            "sections": sections, "citations": {ref: payload["facts"][ref]["value"] for ref in sorted(refs)}}


def validate_plan(plan: dict, payload: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "analysis_id", "instrument", "period", "sections", "citations"}:
        raise ReportValidationError("输出字段不符合报告结构；不接受额外自由文本或数字。")
    if plan["schema_version"] != "1.0" or plan["analysis_id"] != payload["analysis_id"]:
        raise ReportValidationError("报告版本或数据身份不匹配。")
    if plan["instrument"] != payload["instrument"]:
        raise ReportValidationError("股票代码、名称或证券类别不匹配。")
    if plan["period"] != payload["period"]:
        raise ReportValidationError("数据日期不匹配。")
    sections = plan["sections"]
    if not isinstance(sections, dict) or set(sections) != set(payload["required"]):
        raise ReportValidationError("报告必须包含完整的指定分节。")
    seen, references = set(), set()
    for section, keys in sections.items():
        if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(key, str) for key in keys):
            raise ReportValidationError("分节内容必须是受控句式标识列表。")
        for key in keys:
            if key in seen or key not in payload["statements"] or payload["statements"][key]["section"] != section:
                raise ReportValidationError("句式重复、不适用于当前数据或分节错误。")
            seen.add(key)
            references.update(payload["statements"][key]["refs"])
        if not set(payload["required"][section]).issubset(keys):
            raise ReportValidationError("缺少必要的数据、指标或限制说明。")
    citations = plan["citations"]
    if not isinstance(citations, dict) or set(citations) != references:
        raise ReportValidationError("数据引用存在缺失或未经授权的字段。")
    for ref in references:
        expected, actual = payload["facts"][ref]["value"], citations[ref]
        if isinstance(expected, float):
            valid = type(actual) in {float, int} and actual == expected and math.isfinite(actual)
        else:
            valid = type(actual) is type(expected) and actual == expected
        if not valid:
            raise ReportValidationError(f"数据引用与Python结果不一致：{ref}。")
    return plan
