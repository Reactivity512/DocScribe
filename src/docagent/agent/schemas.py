"""JSON-контракт вывода агента (шаг 2).

Один вызов LLM = один target. Модель обязана вернуть ровно этот объект;
всё, что не парсится/валидируется — retry с ошибкой в промпте, затем drop.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from docagent.mcp_server.models import ExpectedDocsTarget


class DraftAction(str, Enum):
    create = "create"          # новый файл/секция (bootstrap)
    append = "append"          # добавить блок после anchor
    update = "update"          # заменить содержимое anchor-секции
    remove = "remove"          # удалить устаревший фрагмент (API удалён)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


class DocDraft(BaseModel):
    """Одна документационная правка."""

    target: ExpectedDocsTarget
    action: DraftAction
    file_path: str                      # напр. docs/api-reference.md или README.md
    anchor: str | None = None           # заголовок/маркер секции, куда вставляется текст
    content_md: str                     # сам markdown-текст правки
    rationale: str = ""                 # почему (попадёт в описание draft PR)
    confidence: float = 0.5
    cited_symbols: list[str] = Field(default_factory=list)  # антигаллюцинация: только из api_snapshot

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("content_md")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content_md is empty")
        return v.strip()


class DraftList(BaseModel):
    drafts: list[DocDraft] = Field(default_factory=list)
    skipped_reason: str | None = None   # если модель решила, что писать нечего


def parse_llm_json(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели (терпимо к ```json ... ``` и prose)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fence.group(1) if fence else None
    if raw is None:
        m = _JSON_BLOCK_RE.search(text)
        raw = m.group(0) if m else None
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def normalize_draft_obj(obj: dict) -> dict:
    """Дешёвая нормализация до pydantic-валидации (экономит retry-циклы на 3b-моделях)."""
    if not isinstance(obj, dict):
        return obj
    a = obj.get("action")
    if isinstance(a, str):
        a_l = a.lower().strip()
        # частые галлюцинации мелких моделей
        alias = {"add": "append", "insert": "append", "modify": "update",
                 "replace": "update", "delete": "remove", "edit": "update",
                 "create_new": "create", "new": "create"}
        obj["action"] = alias.get(a_l, a_l)
    anchor = obj.get("anchor")
    if isinstance(anchor, str):
        s = anchor.strip().strip("`").strip()
        if not s or s.lower() in {"null", "none", "n/a", "-"}:
            obj["anchor"] = None
        else:
            # якорь должен быть заголовком; если модель вернула текст секции — берём первую строку
            first = s.splitlines()[0].strip()
            if not first.startswith("#"):
                first = "## " + first[:80]
            obj["anchor"] = first
    return obj


def validate_draft(obj: dict) -> DocDraft:
    """Pydantic-валидация одного объекта; бросает ValidationError/ValueError."""
    obj = normalize_draft_obj(obj)
    allowed = {f for f in DocDraft.model_fields}
    cleaned = {k: v for k, v in obj.items() if k in allowed}
    return DocDraft.model_validate(cleaned)
