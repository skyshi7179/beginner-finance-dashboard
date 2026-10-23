"""Versioned, local persistence for user-authored strategy rule definitions."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from src.backtest.strategies import RULE_SCHEMA_VERSION, explain_rules, validate_rules


STORE_SCHEMA_VERSION = 1
DEFAULT_ROOT = Path("data/user_strategies")
_SAFE_ID = re.compile(r"^[0-9a-f]{32}$")


class StrategyStoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _validate_text(value, label: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise StrategyStoreError(f"{label}不能为空。")
    if any(character in text for character in "\r\n\0") or len(text) > maximum:
        raise StrategyStoreError(f"{label}包含换行、空字符或超过{maximum}字。")
    return text


def validate_profile(profile: dict) -> dict:
    if not isinstance(profile, dict):
        raise StrategyStoreError("策略文件必须是JSON对象。")
    identifier = str(profile.get("strategy_id") or "")
    if not _SAFE_ID.fullmatch(identifier):
        raise StrategyStoreError("策略标识无效。")
    rules = profile.get("rules")
    validate_rules(rules)
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "rule_schema_version": RULE_SCHEMA_VERSION,
        "strategy_id": identifier,
        "name": _validate_text(profile.get("name"), "策略名称", 80, required=True),
        "description": _validate_text(profile.get("description"), "策略说明", 1000),
        "rules": rules,
        "execution_assumption": "NEXT_OPEN",
        "long_only": True,
        "created_at": str(profile.get("created_at") or _now()),
        "updated_at": str(profile.get("updated_at") or _now()),
    }


def _directory(root: Path, status: str) -> Path:
    if status not in {"active", "recovery"}:
        raise StrategyStoreError("策略目录状态无效。")
    return Path(root) / status


def _path(root: Path, status: str, identifier: str) -> Path:
    if not _SAFE_ID.fullmatch(str(identifier)):
        raise StrategyStoreError("策略标识无效。")
    return _directory(root, status) / f"{identifier}.json"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_strategy(name: str, description: str, rules: dict, *, root: Path = DEFAULT_ROOT,
                  strategy_id: str | None = None, overwrite: bool = False) -> dict:
    identifier = strategy_id or uuid.uuid4().hex
    destination = _path(root, "active", identifier)
    existing = None
    if destination.exists():
        if not overwrite:
            raise StrategyStoreError("同一策略标识已存在；请选择更新或另存。")
        existing = load_strategy(destination)
    profile = validate_profile({
        "strategy_id": identifier,
        "name": name,
        "description": description,
        "rules": rules,
        "created_at": existing.get("created_at") if existing else _now(),
        "updated_at": _now(),
    })
    duplicates = [item for item in list_strategies(root=root, status="active")
                  if item["name"].casefold() == profile["name"].casefold() and item["strategy_id"] != identifier]
    if duplicates:
        raise StrategyStoreError("已有同名策略；请修改名称，或先加载原策略后选择更新。")
    _atomic_json(destination, profile)
    return profile


def load_strategy(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.suffix != ".json":
        raise StrategyStoreError("策略文件不存在或不是普通JSON文件。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyStoreError(f"策略文件无法读取：{exc}") from exc
    if payload.get("schema_version") != STORE_SCHEMA_VERSION:
        raise StrategyStoreError("策略文件版本暂不支持。")
    return validate_profile(payload)


def list_strategies(*, root: Path = DEFAULT_ROOT, status: str = "active") -> list[dict]:
    result = []
    directory = _directory(root, status)
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            profile = load_strategy(path)
            explanation = explain_rules(profile["rules"])
            result.append({**profile, "path": str(path), "buy_summary": explanation["buy"],
                           "sell_summary": explanation["sell"], "status": status})
        except (StrategyStoreError, ValueError):
            continue
    return sorted(result, key=lambda item: item["updated_at"], reverse=True)


def archive_strategy(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    source = _path(root, "active", identifier)
    profile = load_strategy(source)
    destination = _path(root, "recovery", identifier)
    if destination.exists():
        raise StrategyStoreError("回收区已有同一策略标识，未覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return profile


def restore_strategy(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    source = _path(root, "recovery", identifier)
    profile = load_strategy(source)
    destination = _path(root, "active", identifier)
    if destination.exists():
        raise StrategyStoreError("已保存策略中存在同一标识，未覆盖。")
    active_names = {item["name"].casefold() for item in list_strategies(root=root, status="active")}
    if profile["name"].casefold() in active_names:
        raise StrategyStoreError("已保存策略中存在同名策略，请先处理名称冲突。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return profile


def permanently_delete_strategy(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    path = _path(root, "recovery", identifier)
    profile = load_strategy(path)
    path.unlink()
    return profile
