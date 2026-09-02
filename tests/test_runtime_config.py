from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.schemas import ConfigRequest
from config.settings import get_settings
from core.runtime_config import apply_runtime_config


class RecordingClient:
    def __init__(self, *, fail_first: bool = False):
        self.api_key = "old-key"
        self.base_url = "https://old.example/v1"
        self.fail_first = fail_first
        self.calls: list[tuple[str | None, str | None]] = []

    def update_credentials(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.calls.append((api_key, base_url))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("replacement construction failed")
        self.api_key = api_key or ""
        self.base_url = base_url or ""


def _old_environment() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "old-key",
        "OPENAI_BASE_URL": "https://old.example/v1",
        "DEFAULT_MODEL": "old-model",
        "WRITER_MODEL": "old-model",
        "PLANNER_MODEL": "old-model",
        "CRITIC_MODEL": "old-model",
        "STATE_MODEL": "old-model",
        "AUDIT_STRICTNESS": "standard",
    }


def _new_environment() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "new-key",
        "OPENAI_BASE_URL": "https://new.example/v1",
        "DEFAULT_MODEL": "new-model",
        "WRITER_MODEL": "writer-v2",
        "PLANNER_MODEL": "planner-v2",
        "CRITIC_MODEL": "critic-v2",
        "STATE_MODEL": "state-v2",
        "AUDIT_STRICTNESS": "strict",
    }


def test_runtime_config_commits_file_environment_settings_and_client_together():
    old_env = _old_environment()
    updates = _new_environment()
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, old_env, clear=False):
        env_path = Path(tmp) / ".env"
        env_path.write_text("# keep this comment\nUNRELATED=kept\n", encoding="utf-8")
        client = RecordingClient()
        get_settings.cache_clear()
        try:
            apply_runtime_config(env_path, updates, client)
            saved = env_path.read_text(encoding="utf-8")
            assert "# keep this comment" in saved
            assert "UNRELATED=kept" in saved
            assert os.environ["DEFAULT_MODEL"] == "new-model"
            assert get_settings().WRITER_MODEL == "writer-v2"
            assert client.calls == [("new-key", "https://new.example/v1")]
        finally:
            get_settings.cache_clear()


def test_runtime_config_restores_every_layer_when_client_swap_fails():
    old_env = _old_environment()
    updates = _new_environment()
    original = "# exact formatting survives\nOPENAI_BASE_URL='https://file-old.example/v1'\nKEEP=x\n"
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, old_env, clear=False):
        env_path = Path(tmp) / ".env"
        env_path.write_text(original, encoding="utf-8")
        client = RecordingClient(fail_first=True)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="replacement construction failed"):
                apply_runtime_config(env_path, updates, client)

            assert env_path.read_text(encoding="utf-8") == original
            assert {key: os.environ.get(key) for key in old_env} == old_env
            assert get_settings().DEFAULT_MODEL == "old-model"
            assert client.calls == [
                ("new-key", "https://new.example/v1"),
                ("old-key", "https://old.example/v1"),
            ]
            assert client.api_key == "old-key"
            assert client.base_url == "https://old.example/v1"
        finally:
            get_settings.cache_clear()


def test_runtime_config_removes_new_dotenv_when_first_write_must_roll_back():
    old_env = _old_environment()
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, old_env, clear=False):
        env_path = Path(tmp) / ".env"
        client = RecordingClient(fail_first=True)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError):
                apply_runtime_config(env_path, _new_environment(), client)
            assert not env_path.exists()
        finally:
            get_settings.cache_clear()


def test_web_config_noop_preserves_dotenv_and_does_not_reserve_or_swap():
    import web_ui

    current = SimpleNamespace(
        OPENAI_API_KEY="existing-secret",
        OPENAI_BASE_URL="https://same.example/v1/",
        DEFAULT_MODEL="same-model",
        WRITER_MODEL="same-model",
        PLANNER_MODEL="same-model",
        CRITIC_MODEL="same-model",
        STATE_MODEL="same-model",
        AUDIT_STRICTNESS="standard",
    )
    request = ConfigRequest(
        api_key="",
        base_url="https://same.example/v1",
        model="same-model",
        audit_strictness="standard",
    )
    with (
        patch.object(web_ui, "get_settings", return_value=current),
        patch.object(web_ui, "apply_runtime_config") as apply,
        patch.object(web_ui.generation_status, "reserve") as reserve,
    ):
        result = web_ui.update_config(request)

    assert result == {"status": "unchanged", "message": "配置未变化，无需写入。"}
    apply.assert_not_called()
    reserve.assert_not_called()


@pytest.mark.parametrize("original", [
    b"# keep CRLF\r\nOPENAI_BASE_URL='https://old.example/v1'\r\nKEEP=x\r\n\r\n",
    b"\xef\xbb\xbf# keep BOM and no final newline\nKEEP=x",
])
def test_runtime_config_rollback_preserves_exact_original_bytes(tmp_path, original):
    env_path = tmp_path / ".env"
    env_path.write_bytes(original)
    with patch.dict(os.environ, _old_environment(), clear=False):
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError):
                apply_runtime_config(env_path, _new_environment(), RecordingClient(fail_first=True))
            assert env_path.read_bytes() == original
        finally:
            get_settings.cache_clear()


def test_env_edit_keeps_unrelated_bytes_and_newline_style(tmp_path):
    from core.env_file import update_env_file

    env_path = tmp_path / ".env"
    env_path.write_bytes(b'\xef\xbb\xbf# header\r\nMODEL=old\r\nKEEP=" untouched " # note\r\n\r\n')
    update_env_file(env_path, {"MODEL": "new"})
    assert env_path.read_bytes() == b'\xef\xbb\xbf# header\r\nMODEL="new"\r\nKEEP=" untouched " # note\r\n\r\n'


def test_config_comparison_is_serialized_with_the_update():
    import web_ui

    lock_held = False

    class CheckedLock:
        def __enter__(self):
            nonlocal lock_held
            lock_held = True

        def __exit__(self, *_args):
            nonlocal lock_held
            lock_held = False

    def settings_under_lock():
        assert lock_held, "No-op decisions must be made inside the update lock"
        return SimpleNamespace(**_old_environment())

    with (
        patch.object(web_ui, "_CONFIG_ENDPOINT_LOCK", CheckedLock()),
        patch.object(web_ui, "get_settings", side_effect=settings_under_lock),
    ):
        result = web_ui.update_config(ConfigRequest(base_url="https://old.example/v1", model="old-model"))
    assert result["status"] == "unchanged"
