"""Transactional runtime configuration updates.

The dotenv file, process environment and live LLM client form one logical
configuration.  This module keeps them aligned when a live client swap fails.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from config.settings import get_settings
from core.env_file import update_env_file
from core.storage import atomic_write_text


class CredentialClient(Protocol):
    api_key: str
    base_url: str

    def update_credentials(self, api_key: str | None = None, base_url: str | None = None) -> None: ...


class RuntimeConfigRollbackError(RuntimeError):
    """Raised when both a configuration update and its rollback fail."""


_RUNTIME_CONFIG_LOCK = threading.RLock()


def apply_runtime_config(
    env_path: Path,
    updates: Mapping[str, str],
    client: CredentialClient,
) -> None:
    """Atomically align dotenv, ``os.environ``, settings and the live client.

    ``LLMClient.update_credentials`` itself performs an atomic client swap.  If
    any validation or construction step raises, this function restores the
    byte-equivalent dotenv text, the previous process environment and the
    client's previous credentials/settings.
    """
    env_path = Path(env_path)
    normalized = {str(key): str(value) for key, value in updates.items()}
    with _RUNTIME_CONFIG_LOCK:
        original_exists = env_path.exists()
        if original_exists:
            with env_path.open(encoding="utf-8", newline="") as handle:
                original_text = handle.read()
        else:
            original_text = ""
        original_env = {key: os.environ.get(key) for key in normalized}
        old_settings = get_settings()
        old_key = getattr(client, "api_key", old_settings.OPENAI_API_KEY)
        old_url = getattr(client, "base_url", old_settings.OPENAI_BASE_URL)
        client_update_attempted = False

        try:
            update_env_file(env_path, normalized)
            os.environ.update(normalized)
            get_settings.cache_clear()
            new_settings = get_settings()  # Validate the complete effective configuration.
            client_update_attempted = True
            client.update_credentials(
                api_key=new_settings.OPENAI_API_KEY or None,
                base_url=new_settings.OPENAI_BASE_URL or None,
            )
        except Exception as update_error:
            rollback_errors: list[Exception] = []
            try:
                if original_exists:
                    atomic_write_text(env_path, original_text)
                else:
                    env_path.unlink(missing_ok=True)
            except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                rollback_errors.append(exc)

            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            get_settings.cache_clear()

            if client_update_attempted:
                try:
                    client.update_credentials(api_key=old_key or None, base_url=old_url or None)
                except Exception as exc:  # pragma: no cover - client contract violation/fatal close
                    rollback_errors.append(exc)

            if rollback_errors:
                raise RuntimeConfigRollbackError(
                    "运行时配置更新失败，且旧配置未能完整恢复；请重启本机服务"
                ) from update_error
            raise
