"""LLM-бэкенды агента: ollama | vllm | fake (переключение через env DOCAGENT_DOCS_BACKEND).

Ollama и vLLM говорят по OpenAI-совместимому API, поэтому один и тот же клиент.
FakeBackend — детерминированный генератор черновиков из контекста: позволяет
прогонять весь цикл и харнес без запущенной модели (CI, разработка промптов).
"""

from __future__ import annotations

import json
from typing import Protocol

from docagent.config import Settings


class LLMBackend(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        """Синхронный chat completion; бросает исключение при недоступности."""
        ...


class OpenAICompatBackend:
    """Ollama (:11434/v1) и vLLM (:8000/v1) — единый протокол."""

    def __init__(self, settings: Settings, base_url: str | None = None):
        self.s = settings
        self.name = settings.docs_backend  # "ollama" | "vllm"
        default_port = 11434 if self.name == "ollama" else 8000
        self.base_url = base_url or settings.openai_base_url or f"http://localhost:{default_port}/v1"
        self._client = None

    def _get(self):
        if self._client is None:
            from openai import OpenAI  # ленивый импорт

            self._client = OpenAI(base_url=self.base_url, api_key=self.s.api_key,
                                  timeout=self.s.llm_timeout_s)
        return self._client

    def complete(self, system: str, user: str) -> str:
        resp = self._get().chat.completions.create(
            model=self.s.model,
            temperature=self.s.temperature,
            max_tokens=self.s.max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return (resp.choices[0].message.content or "").strip()


class FakeBackend:
    """Детерминированный «черновик из контекста». Не вызывает сеть.

    Формат ответа совпадает с контрактом агента (один JSON-объект DocDraft),
    текст собирается из evidence/summary переданного контекста, поэтому харнес
    может проверять структуру, якоря и антигаллюцинационные проверки end-to-end.
    """

    name = "fake"

    def __init__(self, settings: Settings | None = None):
        self.s = settings

    def complete(self, system: str, user: str) -> str:
        ctx = _parse_fake_context(user)
        draft = _synthesize_draft(ctx)
        return "```json\n" + json.dumps(draft, ensure_ascii=False) + "\n```"


# ------------------------------------------------------------------ helpers --
def _parse_fake_context(user: str) -> dict:
    """Вытаскивает помеченные поля из пользовательского промпта."""
    out: dict[str, str] = {}
    wanted = ("TARGET:", "FILE_PATH:", "ANCHOR:", "SUMMARY:", "EVIDENCE:", "SYMBOLS:", "LANG:")
    for line in user.splitlines():
        for w in wanted:
            if line.startswith(w):
                out[w[:-1]] = line[len(w):].strip()
    return out


def _synthesize_draft(ctx: dict) -> dict:
    target = (ctx.get("TARGET") or "readme").strip().lower()
    lang = (ctx.get("LANG") or "ru").strip().lower()
    summary = ctx.get("SUMMARY", "change detected")
    symbols = [s.strip() for s in (ctx.get("SYMBOLS") or "").split(",") if s.strip()]
    evidence = ctx.get("EVIDENCE", "")
    file_path = ctx.get("FILE_PATH") or {
        "readme": "README.md",
        "api-reference": "docs/api-reference.md",
        "adr": "docs/adr/0001-context.md",
        "guide": "docs/guide.md",
        "changelog": "CHANGELOG.md",
    }.get(target, "docs/notes.md")
    anchor = ctx.get("ANCHOR") or (f"## {symbols[0]}" if symbols else "## Overview")

    if lang == "ru":
        body = (
            f"### Изменения: {summary}\n\n"
            + (f"Затронутые символы: {', '.join(f'`{s}`' for s in symbols)}.\n\n" if symbols else "")
            + (f"Основание: {evidence}.\n" if evidence else "")
        )
        rationale = f"Автосводка по диффу: {summary}"
    else:
        body = (
            f"### Change summary: {summary}\n\n"
            + (f"Affected symbols: {', '.join(f'`{s}`' for s in symbols)}.\n\n" if symbols else "")
            + (f"Evidence: {evidence}.\n" if evidence else "")
        )
        rationale = f"Auto-summary of the diff: {summary}"

    return {
        "target": target,
        "action": "update",
        "file_path": file_path,
        "anchor": anchor,
        "content_md": body,
        "rationale": rationale,
        "confidence": 0.6,
        "cited_symbols": symbols[:10],
    }


def get_backend(settings: Settings) -> object:
    """Фабрика по env: ollama | vllm | fake."""
    backend = settings.docs_backend
    if backend == "fake":
        return FakeBackend(settings)
    if backend in ("ollama", "vllm"):
        return OpenAICompatBackend(settings)
    raise ValueError(f"unknown docs_backend: {backend!r} (expected ollama|vllm|fake)")
