"""Единая точка конфигурации DocAgent (всё через env / .env).

Используется и MCP-сервером (шаг 1), и агентом/оркестратором (шаги 2-3).
Никаких магических констант в коде инструментов — только отсюда.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="DOCAGENT_", extra="ignore"
    )

    # --- пути -----------------------------------------------------------------
    repo_root: Path = REPO_ROOT
    docs_dir: str = "docs"
    gold_dir: Path = REPO_ROOT / "data" / "gold"

    # --- модель (LLM нужен только для verify_api_change) ----------------------
    docs_backend: str = "ollama"  # ollama | vllm | fake
    openai_base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5-coder:3b"
    api_key: str = "not-needed"
    llm_timeout_s: float = 120.0
    temperature: float = 0.1
    max_tokens: int = 700

    docs_lang: str = "ru"  # ru | en

    # --- пороги pre-filter'а (§6 плана шага 0) --------------------------------
    typo_max_changed_lines: int = 6
    typo_min_new_words: int = 8
    max_diff_bytes: int = 200_000
    max_chunk_lines: int = 120  # bootstrap-чанкование

    # --- триггеры -------------------------------------------------------------
    trigger_kinds_enabled: list[str] = Field(
        default_factory=lambda: [
            "api_new",
            "api_signature_changed",
            "api_removed",
            "behavior_changed",
            "adr_new",
            "adr_changed",
            "dependency_added",
            "config_or_env_changed",
            "breaking_change",
            "new_user_feature",
        ]
    )

    @property
    def use_llm(self) -> bool:
        return self.docs_backend in ("ollama", "vllm")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
