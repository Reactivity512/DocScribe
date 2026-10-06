"""Состояние графа оркестратора (Шаг 3).

TypedDict с Annotated-редьюсерами: списки накапливаются через operator.add,
скаляры перезаписываются. Это позволяет нодам возвращать только дельту.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Approval(TypedDict, total=False):
    decision: str          # approve | reject | changes
    feedback: str          # замечания лида (для revise)
    reviewer: str          # GitHub-логин (шаг 5); в шаге 3 — "cli"/"test"


class OrchState(TypedDict, total=False):
    # --- вход ---
    pr_ref: str                      # "owner/repo#123", URL /pull/N, gold-007 или путь к diff
    diff_text: str
    lang: str                        # ru | en (по умолчанию из Settings.docs_lang)
    thread_id: str                   # id ветки исполнения (thread = PR в проде)
    backend_name: str                # ollama | vllm | fake (инжект бэкенда агента)

    # --- шаг 4: метаданные PR, загруженные нодой fetch_pr ---
    pr_slug: str                     # owner/repo
    pr_number: int
    pr_title: str
    pr_body: str
    pr_state: str                    # open | closed
    pr_base: str                     # целевая ветка (main)
    pr_head: str                     # ветка PR-а

    # --- шаг 1 (MCP/rules) ---
    behavior: str                    # stay_silent | write_docs | escalate_bootstrap
    triggers: list[str]
    expected_targets: list[str]
    api_snapshot: list[str]

    # --- шаг 2 (LLM agent) ---
    drafts: list[dict]               # сериализованные DocDraft.model_dump()
    drop_reasons: Annotated[list[dict], operator.add]
    llm_calls: int

    # --- self_check / payload ---
    check_issues: list[str]
    payload: dict[str, Any]          # {files:[{path,content}], commit_message, body_md}

    # --- HITL ---
    approval: Approval
    revisions_count: int             # циклы revise (лимит MAX_REVISIONS)
    attempts_used: int               # ретраи генерации внутри generate_drafts

    # --- результат ---
    outbox_path: str
    pr_number: int                   # номер опубликованного PR (шаг 4)
    pr_url: str                      # ссылка на PR для лида и watcher'а (шаг 5)
    updated: bool                    # публикация = обновление существующего PR (шаг 5)
    publish_target: str              # github | outbox на момент публикации
    review_pending: bool             # PR опубликован и ждёт ревью -> interrupt (шаг 5)
    status: Annotated[list[str], operator.add]   # накопитель статусов; финальный — последний
    events: Annotated[list[dict], operator.add]  # structured log (node, ms, counts)
    errors: Annotated[list[str], operator.add]
