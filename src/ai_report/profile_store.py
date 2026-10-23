"""Non-secret local model profile storage with a recoverable trash."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid


STORE_SCHEMA_VERSION = 1
DEFAULT_ROOT = Path("data/model_profiles")
_SAFE_ID = re.compile(r"^[0-9a-f]{32}$")


class ModelProfileStoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _clean(value, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(character in text for character in "\r\n\0"):
        raise ModelProfileStoreError(f"{label}不能为空、包含换行或超过{maximum}字。")
    return text


def validate_profile(profile: dict) -> dict:
    identifier = str(profile.get("profile_id") or "")
    if not _SAFE_ID.fullmatch(identifier):
        raise ModelProfileStoreError("模型配置标识无效。")
    label = _clean(profile.get("label"), "配置名称", 80)
    model = _clean(profile.get("model"), "模型名称", 120)
    base = _clean(profile.get("base_url"), "API基础地址", 500).rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ModelProfileStoreError("API基础地址必须是无内嵌凭证、查询参数和片段的HTTP(S)地址。")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelProfileStoreError("远程API必须使用HTTPS；HTTP只允许本机服务。")
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "profile_id": identifier,
        "label": label,
        "provider": _clean(profile.get("provider") or "custom", "服务商", 80),
        "model": model,
        "base_url": base,
        "credential_saved": bool(profile.get("credential_saved", False)),
        "created_at": str(profile.get("created_at") or _now()),
        "updated_at": str(profile.get("updated_at") or _now()),
    }


def _path(root: Path, status: str, identifier: str) -> Path:
    if status not in {"active", "recovery"} or not _SAFE_ID.fullmatch(str(identifier)):
        raise ModelProfileStoreError("模型配置路径无效。")
    return Path(root) / status / f"{identifier}.json"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_profile(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.suffix != ".json":
        raise ModelProfileStoreError("模型配置不存在或不是普通JSON文件。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelProfileStoreError(f"模型配置无法读取：{exc}") from exc
    if payload.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ModelProfileStoreError("模型配置版本暂不支持。")
    return validate_profile(payload)


def list_profiles(*, root: Path = DEFAULT_ROOT, status: str = "active") -> list[dict]:
    directory = Path(root) / status
    if status not in {"active", "recovery"} or not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        try:
            result.append({**load_profile(path), "path": str(path), "status": status})
        except ModelProfileStoreError:
            continue
    return sorted(result, key=lambda item: item["updated_at"], reverse=True)


def save_profile(fields: dict, *, root: Path = DEFAULT_ROOT, profile_id: str | None = None,
                 overwrite: bool = False) -> dict:
    identifier = profile_id or uuid.uuid4().hex
    destination = _path(root, "active", identifier)
    existing = None
    if destination.exists():
        if not overwrite:
            raise ModelProfileStoreError("同一模型配置标识已存在；请选择更新或另存。")
        existing = load_profile(destination)
    payload = validate_profile({**fields, "profile_id": identifier,
                                "created_at": existing.get("created_at") if existing else _now(),
                                "updated_at": _now()})
    duplicates = [item for item in list_profiles(root=root) if item["label"].casefold() == payload["label"].casefold()
                  and item["profile_id"] != identifier]
    if duplicates:
        raise ModelProfileStoreError("已有同名模型配置；请修改名称或更新原配置。")
    _atomic_json(destination, payload)
    return payload


def archive_profile(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    source = _path(root, "active", identifier)
    profile = load_profile(source)
    destination = _path(root, "recovery", identifier)
    if destination.exists():
        raise ModelProfileStoreError("模型配置回收区已有同一标识，未覆盖。")
    profile["credential_saved"] = False
    _atomic_json(destination, profile)
    source.unlink()
    return profile


def restore_profile(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    source = _path(root, "recovery", identifier)
    profile = load_profile(source)
    destination = _path(root, "active", identifier)
    if destination.exists():
        raise ModelProfileStoreError("已保存配置中存在同一标识，未覆盖。")
    if profile["label"].casefold() in {item["label"].casefold() for item in list_profiles(root=root)}:
        raise ModelProfileStoreError("已保存配置中存在同名项，请先处理名称冲突。")
    profile["credential_saved"] = False
    _atomic_json(destination, profile)
    source.unlink()
    return profile


def permanently_delete_profile(identifier: str, *, root: Path = DEFAULT_ROOT) -> dict:
    path = _path(root, "recovery", identifier)
    profile = load_profile(path)
    path.unlink()
    return profile
