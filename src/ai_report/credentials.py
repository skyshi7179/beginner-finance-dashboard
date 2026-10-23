"""API credential access through the operating-system keyring only."""

from __future__ import annotations


SERVICE_NAME = "A-Share Research Dashboard"


class CredentialStoreError(RuntimeError):
    pass


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError, NoKeyringError
    except ImportError as exc:
        raise CredentialStoreError("未安装系统凭据组件，当前只能使用会话内API Key。") from exc
    return keyring, (KeyringError, NoKeyringError)


def save_api_key(profile_id: str, api_key: str) -> None:
    if not str(api_key or "").strip():
        raise CredentialStoreError("API Key不能为空。")
    keyring, errors = _keyring()
    try:
        keyring.set_password(SERVICE_NAME, str(profile_id), str(api_key).strip())
    except errors as exc:
        raise CredentialStoreError("系统安全存储不可用，Key未写入项目文件。") from exc


def get_api_key(profile_id: str) -> str | None:
    keyring, errors = _keyring()
    try:
        return keyring.get_password(SERVICE_NAME, str(profile_id))
    except errors as exc:
        raise CredentialStoreError("无法读取系统安全存储中的API Key。") from exc


def delete_api_key(profile_id: str) -> None:
    keyring, errors = _keyring()
    try:
        if keyring.get_password(SERVICE_NAME, str(profile_id)) is not None:
            keyring.delete_password(SERVICE_NAME, str(profile_id))
    except errors as exc:
        raise CredentialStoreError("无法从系统安全存储删除API Key。") from exc
