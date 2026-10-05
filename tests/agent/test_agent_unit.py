"""Unit-тесты агента (шаг 2): парсинг JSON, retry, drop, антигаллюцинация, stay-silent."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.agent.backends import FakeBackend  # noqa: E402
from docagent.agent.guards import check_hallucinated_symbols, validate_draft_full  # noqa: E402
from docagent.agent.loop import DocsAgent  # noqa: E402
from docagent.agent.schemas import DocDraft, parse_llm_json, validate_draft  # noqa: E402
from docagent.config import Settings  # noqa: E402

SETTINGS = Settings(docs_backend="fake")
DIFF_API_NEW = json.loads(
    (REPO / "data/gold/cases/gold-005.json").read_text(encoding="utf-8")
)["code_change"]["diff"]


class ScriptedBackend:
    """Бэкенд, выдающий заранее заготовленные ответы по порядку."""
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self.replies.pop(0) if self.replies else self.replies[-1]


VALID_REPLY = json.dumps({
    "target": "api-reference", "action": "append",
    "file_path": "docs/api-reference.md", "anchor": "## Overview",
    "content_md": "### `get_cached(key, ttl_seconds=300)`\n\nReturns cached value.",
    "rationale": "new public function", "confidence": 0.8,
    "cited_symbols": ["get_cached"],
}, ensure_ascii=False)


def test_parse_llm_json_plain_and_fenced():
    assert parse_llm_json(VALID_REPLY) is not None
    assert parse_llm_json(f"текст\n```json\n{VALID_REPLY}\n```") is not None
    assert parse_llm_json("полный мусор без скобок") is None
    assert parse_llm_json("") is None


def test_validate_draft_drops_unknown_keys():
    obj = json.loads(VALID_REPLY) | {"unknown_field": 1}
    d = validate_draft(obj)
    assert d.target.value == "api-reference"


def test_hallucination_guard_blocks_invented_symbol():
    d = DocDraft(target="api-reference", action="append", file_path="x.md",
                 anchor="## Overview", content_md="Используем `nonexistent_api` тут.")
    bad = check_hallucinated_symbols(d, {"get_cached"})
    assert "nonexistent_api" in bad
    assert validate_draft_full(d, {"get_cached"})  # есть ошибка


def test_hallucination_guard_allows_known():
    d = DocDraft(target="api-reference", action="append", file_path="x.md",
                 anchor="## Overview", content_md="Функция `get_cached` кэширует.")
    assert check_hallucinated_symbols(d, {"get_cached"}) == []


def test_retry_then_success():
    backend = ScriptedBackend(["не json вообще", VALID_REPLY])
    agent = DocsAgent(settings=SETTINGS, repo_path=str(REPO / "fixtures/demopkg"),
                      backend=backend, max_retries=2)
    res = agent.run(DIFF_API_NEW, lang="ru")
    assert len(res.drafts) == 1
    assert backend.calls == 2  # первый ответ отвергнут, второй принят


def test_drop_after_max_retries():
    backend = ScriptedBackend(["мусор", "мусор", "мусор"])
    agent = DocsAgent(settings=SETTINGS, repo_path=str(REPO / "fixtures/demopkg"),
                      backend=backend, max_retries=1)
    res = agent.run(DIFF_API_NEW, lang="ru")
    assert res.drafts == []
    assert res.dropped and "not a JSON object" in res.dropped[0]["error"]
    assert backend.calls == 2


def test_stay_silent_no_llm_calls():
    typo_diff = ("--- a/docs/guide.md\n+++ b/docs/guide.md\n@@\n-Привет\n+Привет мир\n"
                 "--- a/docs/guide.md\n+++ b/docs/guide.md\n@@\n-Ещё текст для порога слов\n"
                 "+Ещё больше новых слов здесь чтобы не пройти порог typonly правила\n")
    backend = ScriptedBackend([VALID_REPLY])
    agent = DocsAgent(settings=SETTINGS, repo_path=str(REPO / "fixtures/demopkg"),
                      backend=backend)
    res = agent.run(typo_diff, lang="ru")
    if res.decision.behavior.value != "write_docs":
        assert res.llm_calls == 0 and res.drafts == []


def test_skipped_reason_is_valid_outcome():
    backend = ScriptedBackend(['{"skipped_reason": "nothing meaningful"}'])
    agent = DocsAgent(settings=SETTINGS, repo_path=str(REPO / "fixtures/demopkg"),
                      backend=backend)
    res = agent.run(DIFF_API_NEW, lang="ru")
    assert res.drafts == [] and res.dropped == []


def test_fake_backend_end_to_end_structure():
    agent = DocsAgent(settings=SETTINGS, repo_path=str(REPO / "fixtures/demopkg"),
                      backend=FakeBackend())
    res = agent.run(DIFF_API_NEW, lang="ru")
    assert res.produced
    d = res.drafts[0]
    assert d.file_path.endswith(".md")
    assert d.anchor and d.anchor.startswith("#")
