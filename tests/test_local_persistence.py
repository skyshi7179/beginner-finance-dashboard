"""Offline tests for saved custom strategies and local model profiles."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.ai_report.credentials import delete_api_key, get_api_key, save_api_key
from src.ai_report.models import ModelCallError, ModelConfig, call_model, call_model_with_overload_retry
from src.ai_report.profile_store import (
    archive_profile,
    list_profiles,
    permanently_delete_profile,
    restore_profile,
    save_profile,
)
from src.backtest.strategies import DEFAULT_RULES
from src.backtest.strategy_store import (
    archive_strategy,
    list_strategies,
    permanently_delete_strategy,
    restore_strategy,
    save_strategy,
)
from test_backtest_page import seed_cache


APP = Path(__file__).resolve().parents[1] / "app.py"


def test_strategy_store_save_archive_restore_and_permanent_delete(tmp_path):
    root = tmp_path / "strategies"
    profile = save_strategy("单条件策略", "离线测试", DEFAULT_RULES, root=root)
    raw = json.loads((root / "active" / f"{profile['strategy_id']}.json").read_text())
    assert raw["name"] == "单条件策略"
    assert len(raw["rules"]["buy"]["conditions"]) == 1
    assert len(raw["rules"]["sell"]["conditions"]) == 1
    active = list_strategies(root=root)
    assert active[0]["buy_summary"] and active[0]["sell_summary"]
    archive_strategy(profile["strategy_id"], root=root)
    assert not list_strategies(root=root)
    assert list_strategies(root=root, status="recovery")[0]["name"] == "单条件策略"
    restore_strategy(profile["strategy_id"], root=root)
    archive_strategy(profile["strategy_id"], root=root)
    permanently_delete_strategy(profile["strategy_id"], root=root)
    assert not list_strategies(root=root, status="recovery")


def test_model_profile_never_contains_api_key_and_recovery_requires_key(tmp_path):
    root = tmp_path / "models"
    profile = save_profile({
        "label": "智谱测试",
        "provider": "zhipu",
        "model": "glm-5.3-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "credential_saved": True,
    }, root=root)
    path = root / "active" / f"{profile['profile_id']}.json"
    text = path.read_text()
    assert "api_key" not in text.lower() and "secret" not in text.lower()
    archive_profile(profile["profile_id"], root=root)
    recovered = list_profiles(root=root, status="recovery")[0]
    assert recovered["credential_saved"] is False
    restored = restore_profile(profile["profile_id"], root=root)
    assert restored["credential_saved"] is False
    archive_profile(profile["profile_id"], root=root)
    permanently_delete_profile(profile["profile_id"], root=root)
    assert not list_profiles(root=root, status="recovery")


def test_credentials_use_backend_without_writing_files(monkeypatch):
    values = {}
    class Fake:
        def set_password(self, service, account, value):
            values[(service, account)] = value
        def get_password(self, service, account):
            return values.get((service, account))
        def delete_password(self, service, account):
            del values[(service, account)]
    import src.ai_report.credentials as credentials
    monkeypatch.setattr(credentials, "_keyring", lambda: (Fake(), (RuntimeError,)))
    save_api_key("a" * 32, "private-test-value")
    assert get_api_key("a" * 32) == "private-test-value"
    delete_api_key("a" * 32)
    assert get_api_key("a" * 32) is None


def test_429_business_code_is_classified_and_key_is_redacted(monkeypatch):
    class Response:
        status_code = 429
        content = b'{"error":{"code":"1305"}}'
        def json(self):
            return {"error": {"code": "1305", "message": "overload secret-value"}}
        def close(self):
            pass
    monkeypatch.setattr("src.ai_report.models.requests.post", lambda *args, **kwargs: Response())
    config = ModelConfig("glm-test", "https://example.invalid/v1", "secret-value")
    with pytest.raises(ModelCallError) as error:
        call_model(config, [{"role": "user", "content": "test"}])
    assert error.value.business_code == "1305"
    assert "访问量过大" in str(error.value)
    assert "secret-value" not in str(error.value)


def test_overload_retry_only_retries_1305(monkeypatch):
    calls = []
    import src.ai_report.models as models
    def overloaded(*args):
        calls.append(1)
        if len(calls) < 3:
            raise ModelCallError("busy", business_code="1305")
        return "{}"
    monkeypatch.setattr(models, "call_model", overloaded)
    waits = []
    result = call_model_with_overload_retry(
        ModelConfig("m", "https://example.invalid/v1", "k"), [],
        delays=(0.1, 0.2), sleeper=waits.append,
    )
    assert result == "{}" and len(calls) == 3 and waits == [0.1, 0.2]


def test_custom_editor_defaults_to_one_buy_and_sell_condition(tmp_path, monkeypatch):
    import src.backtest.page as page
    import src.backtest.strategy_controls as controls
    seed_cache(tmp_path)
    monkeypatch.setattr(page, "DATA_DIR", tmp_path)
    monkeypatch.setattr(controls, "STRATEGY_ROOT", tmp_path / "user_strategies")
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    app.selectbox(key="strategy_kind").select("custom").run()
    counts = {str(item.key): item.value for item in app.number_input if str(item.key).endswith("_count")}
    assert counts["rule_edit_buy_count"] == 1
    assert counts["rule_edit_sell_count"] == 1
    assert not app.exception


def test_custom_strategy_can_be_named_and_saved_locally(tmp_path, monkeypatch):
    import src.backtest.page as page
    import src.backtest.strategy_controls as controls
    seed_cache(tmp_path / "stocks")
    monkeypatch.setattr(page, "DATA_DIR", tmp_path / "stocks")
    monkeypatch.setattr(controls, "STRATEGY_ROOT", tmp_path / "user_strategies")
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_backtest").click().run()
    app.selectbox(key="strategy_kind").select("custom").run()
    next(item for item in app.checkbox if str(item.key).startswith("rules_confirm_")).check().run()
    app.text_input(key="custom_strategy_name").set_value("我的单条件策略").run()
    app.checkbox(key="custom_strategy_save_local").check().run()
    app.button(key="custom_strategy_save_copy").click().run()
    saved = list_strategies(root=tmp_path / "user_strategies")
    assert len(saved) == 1 and saved[0]["name"] == "我的单条件策略"
    assert not app.exception


def test_model_profile_ui_saves_key_outside_json_and_archives_without_key(tmp_path, monkeypatch):
    import src.ai_report.settings as settings
    import src.stock_data.management as management
    model_root = tmp_path / "model_profiles"
    monkeypatch.setattr(settings, "MODEL_PROFILE_ROOT", model_root)
    monkeypatch.setattr(management, "DATA_DIR", tmp_path / "stocks")
    secrets = {}
    monkeypatch.setattr(settings, "save_api_key", lambda identifier, value: secrets.__setitem__(identifier, value))
    monkeypatch.setattr(settings, "get_api_key", lambda identifier: secrets.get(identifier))
    monkeypatch.setattr(settings, "delete_api_key", lambda identifier: secrets.pop(identifier, None))
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_data_management").click().run()
    app.text_input(key="model_api_key").set_value("test-only-secret").run()
    app.checkbox(key="model_save_key_to_keyring").check().run()
    app.button(key="model_profile_save_copy").click().run()
    profiles = list_profiles(root=model_root)
    assert len(profiles) == 1 and profiles[0]["credential_saved"] is True
    identifier = profiles[0]["profile_id"]
    assert secrets[identifier] == "test-only-secret"
    assert "test-only-secret" not in Path(profiles[0]["path"]).read_text()
    next(item for item in app.checkbox if str(item.key).startswith("model_profile_archive_confirm_")).check().run()
    app.button(key="model_profile_archive").click().run()
    assert identifier not in secrets and not list_profiles(root=model_root)
    assert list_profiles(root=model_root, status="recovery")[0]["credential_saved"] is False
    next(item for item in app.checkbox if str(item.key).startswith("model_profile_restore_confirm_")).check().run()
    app.button(key="model_profile_restore").click().run()
    assert list_profiles(root=model_root)[0]["credential_saved"] is False
    assert not app.exception


def test_data_management_archives_and_restores_saved_strategy(tmp_path, monkeypatch):
    import src.stock_data.management as management
    root = tmp_path / "user_strategies"
    profile = save_strategy("数据管理测试策略", "", DEFAULT_RULES, root=root)
    monkeypatch.setattr(management, "DATA_DIR", tmp_path / "stocks")
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="home_enter_data_management").click().run()
    next(item for item in app.checkbox if str(item.key).startswith("strategy_archive_confirm_")).check().run()
    app.button(key="strategy_archive").click().run()
    assert not list_strategies(root=root)
    assert list_strategies(root=root, status="recovery")[0]["strategy_id"] == profile["strategy_id"]
    next(item for item in app.checkbox if str(item.key).startswith("strategy_restore_confirm_")).check().run()
    app.button(key="strategy_restore").click().run()
    assert list_strategies(root=root)[0]["strategy_id"] == profile["strategy_id"]
    assert not app.exception

