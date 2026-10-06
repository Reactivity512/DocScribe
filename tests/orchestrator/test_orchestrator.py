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
def graph(monkeypatch):
    """Свежий Sqlite-чекпоинтер + изолированный outbox на каждый тест.

    Пути берём в существующем data/work (см. _work_file): песочница DSH не даёт
    Python листать каталоги, которые он сам только что создал, а pytest tmp_path
    делает именно это — поэтому фикстура не использует tmp_path.
    """
    outbox = _work_file("outbox")
    monkeypatch.setenv("DOCAGENT_OUTBOX_DIR", str(outbox))
    monkeypatch.delenv("DOCAGENT_PUBLISH_TARGET", raising=False)
    get_settings.cache_clear()
    db = _work_file("cp").with_suffix(".db")
    conn = sqlite3.connect(str(db), check_same_thread=False)
    yield build_graph(SqliteSaver(conn))
    conn.close()
    db.unlink(missing_ok=True)
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
def test_resume_after_process_restart(monkeypatch):
    """Имитация kill процесса: новый saver/граф поверх того же sqlite-файла."""
    monkeypatch.setenv("DOCAGENT_OUTBOX_DIR", str(_work_file("outbox")))
    get_settings.cache_clear()
    db_path = _work_file("cp").with_suffix(".db")
    db = str(db_path)
    tid = "trestart"
    c1 = sqlite3.connect(db, check_same_thread=False)
    interrupted = build_graph(SqliteSaver(c1)).invoke(state_in("gold-005", tid), cfg(tid))
    c1.close()
    assert "__interrupt__" in interrupted

    c2 = sqlite3.connect(db, check_same_thread=False)   # «новый процесс»
    res = build_graph(SqliteSaver(c2)).invoke(
        Command(resume={"decision": "approve", "feedback": "", "reviewer": "lead"}), cfg(tid))
    c2.close()
    db_path.unlink(missing_ok=True)
    get_settings.cache_clear()
    assert res["status"][-1] == "published"


def test_events_logged_per_node(graph):
    res = graph.invoke(state_in("gold-005", "tev"), cfg("tev"))
    nodes = [e["node"] for e in res["events"]]
    assert "fetch_pr" in nodes and "analyze_diff" in nodes
    assert "generate_drafts" in nodes and "prepare_payload" in nodes
    assert all("ms" in e for e in res["events"])


# ------------------------------------------------- шаг 4: fetch_pr идёт в GitHub --
class _FakeGithub:
    """Общий фейк для ноды fetch_pr и для publish (шаг 4)."""

    def __init__(self, *, fail=False, diff_override=None):
        self.fail = fail
        self.diff_override = diff_override
        self.prs = []          # метаданные, запрошенные fetch_pr
        self.created = {}      # kwargs create_pull (проверка draft)
        self.commit = None

    # --- read side (fetch_pr) ---
    def diff_text(self, slug, number):
        if self.fail:
            raise RuntimeError("network down")
        self.prs.append((slug, number))
        return self.diff_override if self.diff_override is not None else gold_diff("gold-005")

    def pull_meta(self, slug, number):
        return {"slug": slug, "number": number, "title": "feat: demo change",
                "body": "тело PR", "author": "dev", "state": "open", "draft": False,
                "merged": False, "base": "main", "head": "feature/x",
                "head_sha": "abc", "changed_files": ["demopkg/client.py"]}

    # --- write side (publish) ---
    def repo(self, slug):
        return self

    def get_git_ref(self, ref):
        from github import GithubException
        raise GithubException(404, {}, None)

    def get_contents(self, path, ref=None):
        from github import GithubException
        raise GithubException(404, {}, None)

    def get_branch(self, name):
        return type("B", (), {"commit": type("C", (), {"sha": "basesha"})()})()

    def create_blob_commit(self, slug, branch, base_ref, files, message):
        self.commit = (slug, branch, base_ref, files, message)
        return "newcommit"

    def open_draft_pr(self, slug, branch, title, body, base="main", draft=False):
        self.created.update({"slug": slug, "branch": branch, "title": title,
                             "body": body, "base": base, "draft": draft})
        return {"number": 7, "html_url": "https://github.com/o/r/pull/7"}


def _github_env(monkeypatch, fake):
    """publish_target=github + подмена клиента.

    Патчим GitHubClient в его модуле: nodes/publisher импортируют его лениво
    внутри функции, поэтому подмена в модуле и есть точка внедрения.
    """
    monkeypatch.setenv("DOCAGENT_PUBLISH_TARGET", "github")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    get_settings.cache_clear()
    from docagent.github_int import client as client_mod
    monkeypatch.setattr(client_mod, "GitHubClient", lambda *a, **k: fake)


def _run_graph(state: dict, tid: str):
    """Прогон графа на свежем Sqlite-чекпоинтере.

    Временные файлы кладём в УЖЕ существующий data/work без создания подкаталогов:
    в песочнице Python не может листать/открывать каталоги, которые сам только что
    создал (ни в %TEMP%, ни в рабочей области), а pytest tmp_path делает именно это.
    """
    import uuid
    work = REPO / "data" / "work"
    work.mkdir(parents=True, exist_ok=True)
    db = work / f"cp-{uuid.uuid4().hex[:8]}.db"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    # кэш настроек обязан быть сброшен ДО прогона: иначе тест подхватит
    # publish_target/env предыдущего теста (lru_cache на get_settings)
    get_settings.cache_clear()
    try:
        return build_graph(SqliteSaver(conn)).invoke(state, cfg(tid))
    finally:
        conn.close()
        db.unlink(missing_ok=True)
        get_settings.cache_clear()


def _work_file(suffix: str) -> Path:
    """Путь под временный файл в существующем data/work (без новых каталогов)."""
    import uuid
    work = REPO / "data" / "work"
    work.mkdir(parents=True, exist_ok=True)
    return work / f"{suffix}-{uuid.uuid4().hex[:8]}"


def test_fetch_pr_loads_diff_and_metadata(monkeypatch):
    """Шаг 4: diff и метаданные PR тянет сама нода, а не CLI-обёртка."""
    fake = _FakeGithub()
    _github_env(monkeypatch, fake)
    res = _run_graph({"pr_ref": "https://github.com/o/r/pull/7",
                      "backend_name": "fake", "lang": "ru", "thread_id": "tgh"}, "tgh")

    assert fake.prs == [("o/r", 7)], "нода обязана сама сходить за diff-ом"
    assert res["pr_slug"] == "o/r" and res["pr_number"] == 7
    assert res["pr_base"] == "main" and res["pr_head"] == "feature/x"
    assert res["diff_text"].strip()
    assert res["__interrupt__"][0].value["pr_ref"] == "https://github.com/o/r/pull/7"


def test_fetch_pr_github_error_does_not_crash_graph(monkeypatch):
    """Сеть/токен недоступны -> понятная ошибка в errors, граф не падает трейсбеком."""
    fake = _FakeGithub(fail=True)
    _github_env(monkeypatch, fake)
    res = _run_graph({"pr_ref": "https://github.com/o/r/pull/7",
                      "backend_name": "fake", "lang": "ru", "thread_id": "tghfail"},
                     "tghfail")

    assert any("fetch_pr" in e for e in res["errors"])
    assert res["status"][-1].startswith("silent:")  # пустой diff -> молчим, не выдумываем
    assert "__interrupt__" not in res


def test_fetch_pr_bad_ref_reports_clear_error(monkeypatch):
    fake = _FakeGithub()
    _github_env(monkeypatch, fake)
    res = _run_graph({"pr_ref": "https://example.com/nonsense",
                      "backend_name": "fake", "thread_id": "tghbad"}, "tghbad")
    assert any("не разобрал ссылку" in e for e in res["errors"])
    assert not fake.prs


def test_outbox_target_never_calls_github(monkeypatch):
    """Регрессия: локальный прогон (outbox) не должен ходить в сеть."""
    monkeypatch.setenv("DOCAGENT_PUBLISH_TARGET", "outbox")
    get_settings.cache_clear()
    from docagent.github_int import client as client_mod

    def _boom(*a, **k):
        raise AssertionError("GitHubClient не должен создаваться при publish_target=outbox")

    monkeypatch.setattr(client_mod, "GitHubClient", _boom)
    diff_file = _work_file("local") .with_suffix(".diff")
    diff_file.write_text(gold_diff("gold-005"), encoding="utf-8")
    try:
        res = _run_graph({"pr_ref": str(diff_file), "backend_name": "fake",
                          "thread_id": "tloc"}, "tloc")
    finally:
        diff_file.unlink(missing_ok=True)
    assert "__interrupt__" in res
    assert not res.get("pr_slug")


def test_github_target_publishes_pr_before_interrupt(monkeypatch):
    """Шаг 5: PR создаётся ДО ожидания ревью — лиду есть что смотреть в diff."""
    fake = _FakeGithub()
    _github_env(monkeypatch, fake)
    outbox = _work_file("outbox")
    monkeypatch.setenv("DOCAGENT_OUTBOX_DIR", str(outbox))
    get_settings.cache_clear()
    res = _run_graph({"pr_ref": "https://github.com/o/r/pull/7",
                      "backend_name": "fake", "lang": "ru", "thread_id": "tpub"}, "tpub")

    assert fake.created, "PR обязан существовать до точки HITL"
    assert fake.created["branch"] == "docagent/tpub"
    assert fake.created["base"] == "main"
    assert fake.created["draft"] is False, "обычный PR: одобрение выражается мержем лида"
    assert res.get("pr_number") == 7 and res.get("pr_url")
    assert "__interrupt__" in res
    assert res["__interrupt__"][0].value["kind"] == "docs_review_request"
    if outbox.exists():
        outbox.unlink()
