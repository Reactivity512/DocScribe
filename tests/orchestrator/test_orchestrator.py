"""Unit/e2e-тесты оркестратора (Шаг 3): маршрутизация, interrupt/resume,
revise-loop с лимитом, reject-путь, чекпоинт-восстановление, outbox-контракт.

Все тесты на backend=fake — без зависимости от Ollama.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from docagent.config import get_settings  # noqa: E402
from docagent.orchestrator.graph import build_graph  # noqa: E402

GOLD = REPO / "data" / "gold" / "cases"


def gold_diff(case_id: str) -> str:
    return json.loads((GOLD / f"{case_id}.json").read_text(encoding="utf-8"))["code_change"]["diff"]


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    """Свежий Sqlite-чекпоинтер + изолированный outbox на каждый тест."""
    monkeypatch.setenv("DOCAGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    get_settings.cache_clear()
    conn = sqlite3.connect(str(tmp_path / "cp.db"), check_same_thread=False)
    yield build_graph(SqliteSaver(conn))
    conn.close()
    get_settings.cache_clear()


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def state_in(case_id: str, tid: str) -> dict:
    return {"pr_ref": case_id, "diff_text": gold_diff(case_id),
            "backend_name": "fake", "lang": "ru", "thread_id": tid}


# ---------------------------------------------------------------- routing --
def test_negative_case_goes_silent(graph):
    neg = next(p.stem for p in sorted(GOLD.glob("*.json"))
               if json.loads(p.read_text(encoding="utf-8"))["expected_behavior"] == "stay_silent")
    res = graph.invoke(state_in(neg, f"t-{neg}"), cfg(f"t-{neg}"))
    assert res["status"][-1].startswith("silent:")
    assert not res.get("drafts")
    assert "__interrupt__" not in res  # человек не дёргается вообще


def test_write_case_interrupts_for_approval(graph):
    res = graph.invoke(state_in("gold-005", "t5"), cfg("t5"))
    assert res.get("behavior") == "write_docs"
    assert "__interrupt__" in res
    intr = res["__interrupt__"][0].value
    assert intr["kind"] == "docs_review_request"
    assert intr["files"], "лид должен видеть список файлов"


# ------------------------------------------------------------ resume paths --
def test_approve_publishes_to_outbox(graph):
    graph.invoke(state_in("gold-005", "ta"), cfg("ta"))
    res = graph.invoke(Command(resume={"decision": "approve", "feedback": "",
                                       "reviewer": "lead"}), cfg("ta"))
    assert res["status"][-1] == "published"
    rec = json.loads(Path(res["outbox_path"]).read_text(encoding="utf-8"))
    assert rec["approval"]["decision"] == "approve"
    payload = rec["payload"]
    assert payload["files"], "payload должен содержать файлы для шага 4"
    assert "commit_message" in payload and "body_md" in payload


def test_reject_finalizes(graph):
    graph.invoke(state_in("gold-005", "tr"), cfg("tr"))
    res = graph.invoke(Command(resume={"decision": "reject",
                                       "feedback": "не нужно сейчас",
                                       "reviewer": "lead"}), cfg("tr"))
    assert res["status"][-1] == "rejected"
    assert not res.get("outbox_path")


def test_changes_triggers_revise_then_approve(graph):
    tid = "tc"
    graph.invoke(state_in("gold-005", tid), cfg(tid))
    r2 = graph.invoke(Command(resume={"decision": "changes",
                                      "feedback": "добавь пример использования",
                                      "reviewer": "lead"}), cfg(tid))
    assert r2.get("revisions_count") == 1
    assert "__interrupt__" in r2, "после revise граф снова ждёт ревью"
    res = graph.invoke(Command(resume={"decision": "approve", "feedback": "",
                                       "reviewer": "lead"}), cfg(tid))
    assert res["status"][-1] == "published"


def test_revision_limit_escalates_and_publishes_with_note(graph):
    tid = "tl"
    graph.invoke(state_in("gold-005", tid), cfg(tid))
    res = None
    for _ in range(3):  # MAX_REVISIONS = 3: три цикла revise проходят штатно
        res = graph.invoke(Command(resume={"decision": "changes",
                                           "feedback": "переписать ещё раз",
                                           "reviewer": "lead"}), cfg(tid))
        assert "__interrupt__" in res
    assert res["revisions_count"] == 3
    # четвёртое решение changes -> лимит исчерпан -> эскалация публикует с пометкой
    res = graph.invoke(Command(resume={"decision": "changes",
                                       "feedback": "всё равно плохо",
                                       "reviewer": "lead"}), cfg(tid))
    assert "needs_human_edit" in res["status"]
    assert res["status"][-1] == "published"
    rec = json.loads(Path(res["outbox_path"]).read_text(encoding="utf-8"))
    assert rec["payload"].get("needs_human_edit") is True


# ----------------------------------------------------- checkpoint survival --
def test_resume_after_process_restart(tmp_path, monkeypatch):
    """Имитация kill процесса: новый saver/граф поверх того же sqlite-файла."""
    monkeypatch.setenv("DOCAGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    get_settings.cache_clear()
    db = str(tmp_path / "cp.db")
    tid = "trestart"
    c1 = sqlite3.connect(db, check_same_thread=False)
    interrupted = build_graph(SqliteSaver(c1)).invoke(state_in("gold-005", tid), cfg(tid))
    c1.close()
    assert "__interrupt__" in interrupted

    c2 = sqlite3.connect(db, check_same_thread=False)   # «новый процесс»
    res = build_graph(SqliteSaver(c2)).invoke(
        Command(resume={"decision": "approve", "feedback": "", "reviewer": "lead"}), cfg(tid))
    c2.close()
    get_settings.cache_clear()
    assert res["status"][-1] == "published"


def test_events_logged_per_node(graph):
    res = graph.invoke(state_in("gold-005", "tev"), cfg("tev"))
    nodes = [e["node"] for e in res["events"]]
    assert "fetch_pr" in nodes and "analyze_diff" in nodes
    assert "generate_drafts" in nodes and "prepare_payload" in nodes
    assert all("ms" in e for e in res["events"])
