"""Ноды графа оркестратора (Шаг 3).

Каждая нода — чистая функция OrchState -> delta. Единственная нода с внешними
записями — publish (stubs в data/outbox/; на шаге 4 её содержимое заменится
созданием draft PR через GitHub API, контракт payload не изменится).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from langgraph.types import interrupt

from ..agent.loop import DocsAgent
from ..agent.schemas import DocDraft
from ..config import get_settings
from ..mcp_server.service import decide_on_diff
from .state import OrchState

MAX_REVISIONS = 3


def _ev(node: str, t0: float, **counts) -> dict:
    return {"node": node, "ms": int((time.monotonic() - t0) * 1000), **counts}


# --------------------------------------------------------------------------
def fetch_pr(state: OrchState) -> dict:
    """Загрузка diff. В MVP diff уже передан в состоянии (gold-кейс / CLI);
    на шаге 4 сюда придёт git-клонирование по pr_ref."""
    t0 = time.monotonic()
    diff = state.get("diff_text") or ""
    if not diff and state.get("pr_ref"):
        p = Path(state["pr_ref"])
        if p.exists():
            diff = p.read_text(encoding="utf-8")
    s = get_settings()
    return {
        "diff_text": diff,
        "lang": state.get("lang") or s.docs_lang,
        "thread_id": state.get("thread_id") or f"pr-{state.get('pr_ref', 'unknown')}",
        "events": [_ev("fetch_pr", t0, chars=len(diff))],
    }


def analyze_diff(state: OrchState) -> dict:
    """Шаг 1: детерминированные правила поверх MCP-сервиса."""
    t0 = time.monotonic()
    decision = decide_on_diff(state["diff_text"], get_settings())
    return {
        "behavior": decision.behavior.value,
        "triggers": [t.value for t in decision.trigger_kinds],
        "expected_targets": [t.value for t in decision.expected_docs_targets],
        "events": [_ev("analyze_diff", t0, behavior=decision.behavior.value,
                       targets=len(decision.expected_docs_targets))],
    }


def route_after_analyze(state: OrchState) -> str:
    if state.get("behavior") == "write_docs" and state.get("expected_targets"):
        return "generate_drafts"
    return "finalize_silent"


# --------------------------------------------------------------------------
def _make_agent(state: OrchState):
    """Бэкенд агента инжектируется через состояние (тесты гоняют FakeBackend/
    Scripted без Ollama; prod использует значение из Settings по env)."""
    s = get_settings()
    backend = None
    if state.get("backend_name"):
        from ..agent.backends import get_backend
        backend = get_backend(s.model_copy(update={"docs_backend": state["backend_name"]}))
    return DocsAgent(s, repo_path=None, backend=backend)


def generate_drafts(state: OrchState) -> dict:
    """Шаг 2: LLM-агент по каждому target'у. Ретрай дропнутых внутри ноды
    (макс. attempts = MAX_ATTEMPTS суммарно на target), лимит извне не нужен:
    retry дешёвый и stateless."""
    t0 = time.monotonic()
    agent = _make_agent(state)
    max_attempts = 2
    drafts: list[dict] = []
    drops: list[dict] = []
    llm_calls = 0
    for target_val in state["expected_targets"]:
        # один target за вызов: переиспользуем агентский цикл точечно
        res = agent.run(state["diff_text"], lang=state.get("lang"))
        llm_calls += len(res.trace)
        got = [d.model_dump(mode="json") for d in res.drafts
               if d.target.value == target_val]
        dropped_here = [x for x in res.dropped if x.get("target") == target_val]
        attempt = 1
        while not got and dropped_here and attempt < max_attempts:
            res = agent.run(state["diff_text"], lang=state.get("lang"))
            llm_calls += len(res.trace)
            attempt += 1
            got = [d.model_dump(mode="json") for d in res.drafts
                   if d.target.value == target_val]
            dropped_here = [x for x in res.dropped if x.get("target") == target_val]
        drafts.extend(got)
        if not got and dropped_here:
            drops.extend(dropped_here)
    status = ["drafts_ok"] if drafts else (["all_dropped"] if drops else ["no_drafts"])
    return {
        "drafts": drafts,
        "drop_reasons": drops,
        "llm_calls": llm_calls,
        "attempts_used": max_attempts,
        "status": status,
        "events": [_ev("generate_drafts", t0, drafts=len(drafts),
                       dropped=len(drops), llm_calls=llm_calls)],
    }


def route_after_generate(state: OrchState) -> str:
    return "self_check" if state.get("drafts") else "human_approval"


# --------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)


def self_check(state: OrchState) -> dict:
    """Дешёвые детерминированные проверки качества черновиков перед отправкой
    человеку. Не вызывает LLM. Проблемы не блокируют — они попадают в тело PR
    как предупреждения (лид видит их на ревью)."""
    t0 = time.monotonic()
    issues: list[str] = []
    for d in state.get("drafts", []):
        content = d.get("content_md", "")
        path = d.get("file_path", "?")
        if len(content.strip()) < 40:
            issues.append(f"{path}: слишком короткий черновик (<40 симв.)")
        if not _HEADING_RE.search(content) and d.get("action") != "delete":
            issues.append(f"{path}: нет markdown-заголовка в вставке")
        if d.get("action") == "replace_section" and not d.get("anchor"):
            issues.append(f"{path}: replace_section без anchor")
    return {"check_issues": issues,
            "events": [_ev("self_check", t0, issues=len(issues))]}


def prepare_payload(state: OrchState) -> dict:
    """Собирает единый payload для publish (шаг 4 съест его без изменений)."""
    t0 = time.monotonic()
    files = [{"path": d["file_path"], "content_md": d["content_md"],
              "action": d["action"], "anchor": d.get("anchor"),
              "target": d["target"]} for d in state.get("drafts", [])]
    targets = sorted({f["target"] for f in files})
    body_lines = [f"- `{f['path']}` ({f['action']}, target: {f['target']})"
                  for f in files]
    warn = ""
    if state.get("check_issues"):
        warn = ("\n### ⚠️ Предупреждения self-check\n"
                + "\n".join(f"- {i}" for i in state["check_issues"]) + "\n")
    body_md = (
        f"## 📝 Docs update proposal (автосген, шаг {state.get('revisions_count', 0)})\n\n"
        f"PR: `{state.get('pr_ref', '?')}` · язык: {state.get('lang', 'ru')}\n\n"
        "### Изменяемые файлы\n" + "\n".join(body_lines) + warn +
        "\n_Одобри этот draft PR (review → Approve) или оставь замечания — я перепишу._\n"
    )
    commit = f"docs: автообновление документации ({', '.join(targets)})"
    payload = {"pr_ref": state.get("pr_ref", ""), "branch": "docagent/auto-docs",
               "commit_message": commit, "body_md": body_md, "files": files}
    return {"payload": payload,
            "events": [_ev("prepare_payload", t0, files=len(files))]}


# --------------------------------------------------------------------------
def human_approval(state: OrchState) -> dict:
    """Точка HITL. interrupt() замораживает граф в чекпоинте; решение приходит
    через Command(resume={decision, feedback, reviewer}). В шаге 5 resume будет
    дёргаться webhook-обработчиком комментариев GitHub — здесь ничего не меняется."""
    request = {
        "kind": "docs_review_request",
        "pr_ref": state.get("pr_ref", ""),
        "thread_id": state.get("thread_id", ""),
        "files": [f["path"] for f in state.get("payload", {}).get("files", [])],
        "warnings": state.get("check_issues", []),
        "revision": state.get("revisions_count", 0),
        "question": "approve | reject | changes (+feedback)",
    }
    decision: dict = interrupt(request)  # <- здесь граф ждёт человека
    return {"approval": {"decision": decision.get("decision", "reject"),
                         "feedback": decision.get("feedback", ""),
                         "reviewer": decision.get("reviewer", "unknown")},
            "events": [_ev("human_approval", time.monotonic(),
                           decision=decision.get("decision", "?"))]}


def route_after_approval(state: OrchState) -> str:
    dec = state.get("approval", {}).get("decision", "reject")
    if dec == "approve":
        return "publish"
    if dec == "changes":
        return "revise" if state.get("revisions_count", 0) < MAX_REVISIONS \
            else "escalate_manual"
    return "finalize_rejected"


# --------------------------------------------------------------------------
def revise(state: OrchState) -> dict:
    """Переписывание черновиков по замечаниям лида. MVP: замечания добавляются
    в контекст промпта как требование, генерация заново по всем target'ам."""
    t0 = time.monotonic()
    feedback = state.get("approval", {}).get("feedback", "")
    agent = _make_agent(state)
    diff_ctx = state["diff_text"] + f"\n\n# ЗАМЕЧАНИЯ РЕВЬЮЕРА (учесть обязательно):\n{feedback}"
    drafts: list[dict] = []
    drops: list[dict] = []
    for target_val in state["expected_targets"]:
        res = agent.run(diff_ctx, lang=state.get("lang"))
        drafts += [d.model_dump(mode="json") for d in res.drafts
                   if d.target.value == target_val]
        drops += [x for x in res.dropped if x.get("target") == target_val]
    n = state.get("revisions_count", 0) + 1
    return {"drafts": drafts, "drop_reasons": drops, "revisions_count": n,
            "approval": {}, "status": [f"revised_{n}"],
            "events": [_ev("revise", t0, drafts=len(drafts), revision=n)]}


def escalate_manual(state: OrchState) -> dict:
    """Лимит циклов правок исчерпан: публикуем как есть с пометкой needs_human_edit."""
    t0 = time.monotonic()
    note = ("⛔ Достигнут лимит автоправок "
            f"({MAX_REVISIONS}). Требуется ручная правка человеком.")
    payload = dict(state.get("payload", {}))
    payload["needs_human_edit"] = True
    payload["body_md"] = payload.get("body_md", "") + f"\n---\n{note}\n"
    return {"payload": payload, "status": ["needs_human_edit"],
            "errors": [note], "events": [_ev("escalate_manual", t0)]}


# --------------------------------------------------------------------------
def publish(state: OrchState) -> dict:
    """Единственная side-effect нода.

    Режимы (env DOCAGENT_PUBLISH_TARGET):
      outbox  — шаг 3: запись payload в data/outbox/<thread>.json (дефолт, для тестов);
      github  — шаг 4: создание ветки/коммита/draft PR через GitHub API.
    Контракт payload не меняется между режимами.
    """
    t0 = time.monotonic()
    s = get_settings()
    tid = re.sub(r"[^A-Za-z0-9_.-]", "_", state.get("thread_id", "run"))

    if getattr(s, "publish_target", "outbox") == "github":
        from ..github_int.publisher import publish_to_github
        result = publish_to_github(state, s)
        return {**result, "status": ["published"],
                "events": [_ev("publish", t0, **{k: v for k, v in result.items()
                                                 if k != "status"})]}

    outbox = Path(s.outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{tid}.json"
    record = {
        "payload": state.get("payload", {}),
        "approval": state.get("approval", {}),
        "status_chain": state.get("status", []),
        "revisions": state.get("revisions_count", 0),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"outbox_path": str(path), "status": ["published"],
            "events": [_ev("publish", t0, outbox=str(path))]}


def finalize_silent(state: OrchState) -> dict:
    t0 = time.monotonic()
    reason = state.get("behavior", "stay_silent")
    return {"status": [f"silent:{reason}"],
            "events": [_ev("finalize_silent", t0, reason=reason)]}


def finalize_rejected(state: OrchState) -> dict:
    t0 = time.monotonic()
    fb = state.get("approval", {}).get("feedback", "")
    return {"status": ["rejected"], "errors": [f"rejected by reviewer: {fb[:200]}"],
            "events": [_ev("finalize_rejected", t0)]}
