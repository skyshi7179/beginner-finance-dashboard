"""Explicit model configuration with optional OS-keyring persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

import streamlit as st

from src.ai_report.credentials import CredentialStoreError, delete_api_key, get_api_key, save_api_key
from src.ai_report.models import ModelCallError, call_model, configured_models
from src.ai_report.profile_store import (
    ModelProfileStoreError,
    archive_profile,
    list_profiles,
    permanently_delete_profile,
    restore_profile,
    save_profile,
)


RUNTIME_MODEL_KEY = "runtime_model_profile"
MODEL_PROFILE_ROOT = Path("data/model_profiles")
ZHIPU_47_PRESET = {
    "provider": "zhipu",
    "label": "智谱 GLM-4.7-Flash",
    "model": "glm-4.7-flash",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
}
ZHIPU_53_PRESET = {
    "provider": "zhipu",
    "label": "智谱 GLM-5.3-Flash",
    "model": "glm-5.3-flash",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
}
PROVIDER_OPTIONS = [
    "智谱 GLM-4.7-Flash（预设）",
    "智谱 GLM-5.3-Flash（预设）",
    "自定义 OpenAI 兼容 API",
]


def _secrets() -> dict:
    values = {}
    try:
        for key in ("RESEARCH_LLM_MODEL", "RESEARCH_LLM_BASE_URL", "RESEARCH_LLM_API_KEY"):
            value = st.secrets.get(key)
            if value:
                values[key] = value
    except FileNotFoundError:
        pass
    return values


def _saved_runtime_profiles() -> tuple[list[dict], list[str]]:
    profiles, errors = [], []
    for profile in list_profiles(root=MODEL_PROFILE_ROOT):
        if not profile.get("credential_saved"):
            continue
        try:
            key = get_api_key(profile["profile_id"])
            if key:
                profiles.append({**profile, "api_key": key})
            else:
                errors.append(f"{profile['label']}：本地配置存在，但系统安全存储中没有API Key。")
        except CredentialStoreError as exc:
            errors.append(f"{profile['label']}：{exc}")
    return profiles, errors


def load_models():
    saved, storage_errors = _saved_runtime_profiles()
    runtime = []
    session = st.session_state.get(RUNTIME_MODEL_KEY)
    if session:
        runtime.append(session)
    runtime.extend(saved)
    models, errors = configured_models(secrets=_secrets(), runtime=runtime)
    return models, [*storage_errors, *errors]


def _mode_for(profile: dict) -> str:
    if profile.get("provider") == "zhipu" and profile.get("model") == "glm-4.7-flash":
        return PROVIDER_OPTIONS[0]
    if profile.get("provider") == "zhipu" and profile.get("model") == "glm-5.3-flash":
        return PROVIDER_OPTIONS[1]
    return PROVIDER_OPTIONS[2]


def _apply_pending_profile() -> None:
    if st.session_state.pop("pending_model_session_clear", False):
        st.session_state.pop(RUNTIME_MODEL_KEY, None)
        st.session_state.pop("model_loaded_profile_id", None)
        for widget_key in ("model_api_key", "model_custom_label", "model_custom_name", "model_custom_base_url"):
            st.session_state.pop(widget_key, None)
    profile = st.session_state.pop("pending_model_profile_load", None)
    if not profile:
        return
    key = ""
    if profile.get("credential_saved"):
        try:
            key = get_api_key(profile["profile_id"]) or ""
        except CredentialStoreError as exc:
            st.session_state.model_config_notice = {"errors": [str(exc)]}
    for widget_key in ("model_provider_mode", "model_api_key", "model_custom_label",
                       "model_custom_name", "model_custom_base_url"):
        st.session_state.pop(widget_key, None)
    st.session_state.model_provider_mode = _mode_for(profile)
    st.session_state.model_custom_label = profile["label"]
    st.session_state.model_custom_name = profile["model"]
    st.session_state.model_custom_base_url = profile["base_url"]
    st.session_state.model_api_key = key
    st.session_state.model_loaded_profile_id = profile["profile_id"]
    if key:
        st.session_state[RUNTIME_MODEL_KEY] = {**profile, "api_key": key}
    else:
        st.session_state.pop(RUNTIME_MODEL_KEY, None)
        st.session_state.model_config_notice = {
            "warnings": [f"已加载“{profile['label']}”；请重新输入API Key后再调用。"]
        }


def _candidate() -> dict:
    mode = st.session_state.get("model_provider_mode", PROVIDER_OPTIONS[0])
    if mode == PROVIDER_OPTIONS[0]:
        return {**ZHIPU_47_PRESET, "api_key": st.session_state.get("model_api_key", "")}
    if mode == PROVIDER_OPTIONS[1]:
        return {**ZHIPU_53_PRESET, "api_key": st.session_state.get("model_api_key", "")}
    return {
        "provider": "custom",
        "label": st.session_state.get("model_custom_label", "自定义模型"),
        "model": st.session_state.get("model_custom_name", ""),
        "base_url": st.session_state.get("model_custom_base_url", ""),
        "api_key": st.session_state.get("model_api_key", ""),
    }


def _clear_session_model() -> None:
    st.session_state.pop(RUNTIME_MODEL_KEY, None)
    st.session_state.pop("model_loaded_profile_id", None)
    for key in ("model_api_key", "model_custom_label", "model_custom_name", "model_custom_base_url"):
        st.session_state.pop(key, None)
    st.session_state.model_config_notice = {"success": ["本次会话中的API配置已清除；本地配置和系统钥匙串未删除。"]}


def _notice() -> None:
    notice = st.session_state.pop("model_config_notice", None)
    if not notice:
        return
    if isinstance(notice, str):
        st.success(notice)
        return
    for value in notice.get("success", []):
        st.success(value)
    for value in notice.get("warnings", []):
        st.warning(value)
    for value in notice.get("errors", []):
        st.error(value)


def _save_local(candidate: dict, *, update: bool) -> None:
    valid, errors = configured_models(environ={}, secrets={}, runtime=candidate)
    if not valid:
        st.error(errors[0] if errors else "模型配置不完整。")
        return
    loaded_id = st.session_state.get("model_loaded_profile_id")
    identifier = loaded_id if update else uuid.uuid4().hex
    if update and not identifier:
        st.error("当前没有从本地加载的配置，不能更新；请使用“另存为本地配置”。")
        return
    persist_key = bool(st.session_state.get("model_save_key_to_keyring"))
    previous = next((item for item in list_profiles(root=MODEL_PROFILE_ROOT)
                     if item["profile_id"] == identifier), None)
    fields = {key: candidate[key] for key in ("label", "provider", "model", "base_url")}
    fields["credential_saved"] = False
    try:
        profile = save_profile(fields, root=MODEL_PROFILE_ROOT, profile_id=identifier, overwrite=update)
        if persist_key:
            try:
                save_api_key(identifier, candidate["api_key"])
                try:
                    profile = save_profile({**fields, "credential_saved": True}, root=MODEL_PROFILE_ROOT,
                                           profile_id=identifier, overwrite=True)
                except (OSError, ModelProfileStoreError):
                    delete_api_key(identifier)
                    raise
            except CredentialStoreError as exc:
                st.session_state.model_config_notice = {
                    "warnings": [f"模型配置已保存，但API Key未写入系统安全存储：{exc}"]
                }
        elif update:
            if previous and previous.get("credential_saved"):
                try:
                    delete_api_key(identifier)
                except CredentialStoreError:
                    save_profile({**fields, "credential_saved": True}, root=MODEL_PROFILE_ROOT,
                                 profile_id=identifier, overwrite=True)
                    raise
        st.session_state.model_loaded_profile_id = identifier
        st.session_state[RUNTIME_MODEL_KEY] = candidate
        if "model_config_notice" not in st.session_state:
            key_text = "，API Key已保存到系统钥匙串" if profile.get("credential_saved") else "；API Key仅保留在本次会话"
            st.session_state.model_config_notice = {"success": [f"已保存本地配置“{profile['label']}”{key_text}。"]}
        st.rerun()
    except (OSError, ModelProfileStoreError, CredentialStoreError) as exc:
        st.error(f"本地配置保存失败：{exc}")


def _render_editor() -> None:
    stored = st.session_state.get(RUNTIME_MODEL_KEY, {})
    if "model_provider_mode" not in st.session_state:
        st.session_state.model_provider_mode = _mode_for(stored) if stored else PROVIDER_OPTIONS[0]
    mode = st.radio("API类型", PROVIDER_OPTIONS, horizontal=True, key="model_provider_mode")
    if mode == PROVIDER_OPTIONS[0]:
        st.code("模型：glm-4.7-flash\nAPI基础地址：https://open.bigmodel.cn/api/paas/v4", language="text")
        st.caption("使用模型标准API Key；高峰期可能出现1305模型过载。")
    elif mode == PROVIDER_OPTIONS[1]:
        st.code("模型：glm-5.3-flash\nAPI基础地址：https://open.bigmodel.cn/api/paas/v4", language="text")
        st.caption("使用模型标准API Key；资源包、可用额度与并发以账户控制台为准。")
    else:
        a, b = st.columns(2)
        a.text_input("配置名称", value=stored.get("label", "自定义模型") if stored.get("provider") == "custom" else "自定义模型",
                     key="model_custom_label")
        b.text_input("模型名称", value=stored.get("model", "") if stored.get("provider") == "custom" else "",
                     key="model_custom_name")
        st.text_input("API基础地址", value=stored.get("base_url", "") if stored.get("provider") == "custom" else "",
                      placeholder="https://example.com/v1", key="model_custom_base_url",
                      help="填写到版本路径，系统会追加 /chat/completions；远程地址必须使用HTTPS。")
    st.text_input("API Key", value=stored.get("api_key", ""), type="password", key="model_api_key",
                  help="仅用于明确的模型调用；不会写入模型JSON、日志或Git。")
    candidate = _candidate()
    valid, validation_errors = configured_models(environ={}, secrets={}, runtime=candidate)
    save_col, test_col, clear_col = st.columns(3)
    if save_col.button("保存到本次会话", key="model_config_save", type="primary", use_container_width=True):
        if valid:
            st.session_state[RUNTIME_MODEL_KEY] = candidate
            st.session_state.model_config_notice = {"success": ["配置已保存到本次会话；尚未发起网络请求。"]}
            st.rerun()
        else:
            st.error(validation_errors[0] if validation_errors else "模型配置不完整。")
    if test_col.button("测试连接（调用1次）", key="model_config_test", disabled=not valid, use_container_width=True):
        try:
            with st.spinner("正在进行一次最小JSON响应测试…"):
                content = call_model(valid[0], [
                    {"role": "system", "content": "只返回JSON对象。"},
                    {"role": "user", "content": "返回 {\"ok\": true}，不要添加其他内容。"},
                ])
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("响应不是JSON对象")
            st.success(f"连接成功：{valid[0].display_name} 返回了可解析的JSON对象。")
        except (ModelCallError, ValueError, TypeError) as exc:
            st.error(f"连接测试失败：{exc}")
    clear_col.button("清除会话配置", key="model_config_clear", disabled=not stored,
                     use_container_width=True, on_click=_clear_session_model)

    st.divider()
    st.markdown("#### 保存为本机模型配置")
    st.checkbox("同时把API Key安全保存到本机系统钥匙串", key="model_save_key_to_keyring",
                help="不勾选时只保存名称、模型和地址，Key在关闭本次会话后消失。Key不会进入项目JSON或回收区。")
    update_col, copy_col = st.columns(2)
    if update_col.button("更新已加载的本地配置", key="model_profile_update",
                         disabled=not valid or not st.session_state.get("model_loaded_profile_id"), use_container_width=True):
        _save_local(candidate, update=True)
    if copy_col.button("另存为本地配置", key="model_profile_save_copy",
                       disabled=not valid, use_container_width=True):
        _save_local(candidate, update=False)
    st.caption("删除本地配置时，系统钥匙串中的Key会立即删除；恢复配置后必须重新输入Key。")


def _profile_table(items: list[dict]) -> list[dict]:
    return [{
        "配置名称": item["label"],
        "模型": item["model"],
        "服务商": item["provider"],
        "API基础地址": item["base_url"],
        "API Key状态": "已安全保存" if item.get("credential_saved") else "需要重新输入",
        "修改时间": item["updated_at"],
    } for item in items]


def _render_saved_profiles() -> None:
    active = list_profiles(root=MODEL_PROFILE_ROOT)
    if not active:
        st.info("尚未保存本地模型配置。")
        return
    st.dataframe(_profile_table(active), hide_index=True, width="stretch")
    lookup = {item["profile_id"]: item for item in active}
    selected = st.multiselect("选择本地模型配置", list(lookup), default=list(lookup)[:1],
                              format_func=lambda value: f"{lookup[value]['label']} · {lookup[value]['model']}",
                              key="model_profile_active_selection")
    load_col, archive_col = st.columns(2)
    if load_col.button("加载到配置编辑器", key="model_profile_load", disabled=len(selected) != 1,
                       use_container_width=True):
        st.session_state.pending_model_profile_load = lookup[selected[0]]
        st.rerun()
    digest = hashlib.sha256("|".join(sorted(selected)).encode()).hexdigest()[:12]
    confirmed = st.checkbox(
        f"我确认将所选{len(selected)}项移入回收区；已保存的API Key会立即从系统钥匙串删除",
        key=f"model_profile_archive_confirm_{digest}", disabled=not selected)
    if archive_col.button("批量移入回收区", key="model_profile_archive",
                          disabled=not selected or not confirmed, use_container_width=True):
        outcome = {"success": [], "errors": []}
        for identifier in selected:
            item = lookup[identifier]
            try:
                if item.get("credential_saved"):
                    delete_api_key(identifier)
                archived = archive_profile(identifier, root=MODEL_PROFILE_ROOT)
                outcome["success"].append(f"已移入回收区：{archived['label']}；API Key未进入回收区。")
                if st.session_state.get("model_loaded_profile_id") == identifier:
                    st.session_state.pending_model_session_clear = True
            except (OSError, ModelProfileStoreError, CredentialStoreError) as exc:
                outcome["errors"].append(f"{item['label']}：{exc}")
        st.session_state.model_config_notice = outcome
        st.rerun()


def _render_profile_recovery() -> None:
    items = list_profiles(root=MODEL_PROFILE_ROOT, status="recovery")
    if not items:
        st.info("模型配置回收区为空。")
        return
    st.dataframe(_profile_table(items), hide_index=True, width="stretch")
    lookup = {item["profile_id"]: item for item in items}
    selected = st.multiselect("选择回收区模型配置", list(lookup), default=list(lookup)[:1],
                              format_func=lambda value: f"{lookup[value]['label']} · {lookup[value]['model']}",
                              key="model_profile_recovery_selection")
    digest = hashlib.sha256("|".join(sorted(selected)).encode()).hexdigest()[:12]
    restore_confirm = st.checkbox(f"我确认恢复所选{len(selected)}项；恢复后需要重新输入API Key",
                                  key=f"model_profile_restore_confirm_{digest}", disabled=not selected)
    if st.button("批量恢复模型配置", key="model_profile_restore", disabled=not selected or not restore_confirm):
        outcome = {"success": [], "errors": []}
        for identifier in selected:
            try:
                profile = restore_profile(identifier, root=MODEL_PROFILE_ROOT)
                outcome["success"].append(f"已恢复：{profile['label']}；请重新输入API Key。")
            except (OSError, ModelProfileStoreError) as exc:
                outcome["errors"].append(f"{lookup[identifier]['label']}：{exc}")
        st.session_state.model_config_notice = outcome
        st.rerun()
    with st.expander("高级：永久清空所选回收条目"):
        permanent_confirm = st.checkbox("我理解永久删除后无法恢复", key=f"model_profile_delete_confirm_{digest}",
                                        disabled=not selected)
        if st.button("永久删除所选模型配置", key="model_profile_delete_permanent",
                     disabled=not selected or not permanent_confirm):
            outcome = {"success": [], "errors": []}
            for identifier in selected:
                try:
                    profile = permanently_delete_profile(identifier, root=MODEL_PROFILE_ROOT)
                    outcome["success"].append(f"已永久删除：{profile['label']}。")
                except (OSError, ModelProfileStoreError) as exc:
                    outcome["errors"].append(f"{lookup[identifier]['label']}：{exc}")
            st.session_state.model_config_notice = outcome
            st.rerun()


def render_model_settings() -> None:
    _apply_pending_profile()
    st.subheader("模型与 API")
    st.write("为策略规则起草和研究报告选择模型。输入、切换、保存配置和刷新页面都不会调用API。")
    st.warning("API Key默认只保存在当前会话；选择“系统钥匙串”后才会安全保存在本机，绝不会写进模型JSON或回收区。")
    _notice()
    editor_tab, saved_tab, recovery_tab = st.tabs(["配置与测试", "已保存配置", "配置回收区"])
    with editor_tab:
        _render_editor()
        models, errors = load_models()
        for error in errors:
            st.warning(error)
        if models:
            st.caption("当前可选配置：" + "、".join(model.display_name for model in models) + "。配置完整不代表网络一定可用，可手动测试。")
        else:
            st.info("当前没有完整模型配置；本地规则报告和手动策略编辑仍可正常使用。")
    with saved_tab:
        _render_saved_profiles()
    with recovery_tab:
        _render_profile_recovery()

