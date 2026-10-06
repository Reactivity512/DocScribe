"""Тесты шага 5: классификация комментариев и watcher HITL-цикла (без сети)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.config import Settings, get_settings  # noqa: E402
from docagent.github_int import review, watcher  # noqa: E402
from docagent.github_int.review import (  # noqa: E402
    CHANGES,
    CLARIFY,
    OBJECTION,
    QUESTION,
)

# Существующий каталог: в песочнице DSH Python не может листать/открывать каталоги,
# которые сам только что создал, поэтому временные файлы кладём сюда без подкаталогов.
WORK = REPO / "data" / "work"


# ------------------------------------------------------------- классификация --
@pytest.mark.parametrize("body,expected", [
    # явная просьба изменить
    ("добавь пример вызова", CHANGES),
    ("Поправьте формулировку про retry", CHANGES),
    ("пожалуйста, перепиши раздел про ошибки", CHANGES),
    ("нужно дополнить раздел про токены", CHANGES),
    ("please add a usage example", CHANGES),
    ("could you update the error section?", CHANGES),
    ("should be mentioned in README", CHANGES),
    ("remove the outdated note", CHANGES),
    # возражение -> сначала объясняем
    ("это неверно, у нас другой контракт", OBJECTION),
    ("тут ошибка в описании параметра", OBJECTION),
    ("this is incorrect", OBJECTION),
    ("не согласен с формулировкой", OBJECTION),
    # вопрос -> отвечаем, но не переписываем
    ("почему вы решили, что это публичный API?", QUESTION),
    ("зачем тут пример?", QUESTION),
    ("why did the bot touch api-reference?", QUESTION),
    ("откуда взялся параметр ttl_seconds?", QUESTION),
    # непонятный комментарий -> уточняем
    ("спасибо, посмотрю позже", CLARIFY),
    ("хм", CLARIFY),
    ("", CLARIFY),
    # защита от ложных срабатываний подстрок
    ("обновил address в конфиге, посмотри", CLARIFY),
    ("fixed the fixture yesterday", CLARIFY),
])
def test_classify_comment(body, expected):
    assert review.classify_comment(body).kind == expected


def test_changes_detected_even_with_objection():
    """«неверно, добавь пример» — это просьба изменить: правка важнее объяснения."""
    intent = review.classify_comment("неверно, добавь пример в README")
    assert intent.kind == CHANGES


def test_reply_never_empty_and_mentions_author():
    state = {"pr_ref": "https://github.com/o/r/pull/7", "expected_targets": ["api-reference"],
             "payload": {"files": [{"path": "docs/api-reference.md",
                                    "target": "api-reference"}]}}
    for kind in (CHANGES, OBJECTION, QUESTION, CLARIFY):
        txt = review.reply_for(review.Intent(kind, "t"), state, {"author": "lead"})
        assert txt.strip() and "@lead" in txt
    assert "docs/api-reference.md" in review.reply_for(
        review.Intent(CHANGES, "t"), state, {"author": "lead"})


# ------------------------------------------------------------------- watcher --
class FakeSnapshot:
    def __init__(self, values): self.values = values


class FakeGraph:
    """state -> resume -> новое состояние (как checkpoint-граф, но без sqlite)."""

    def __init__(self, state=None):
        self.state = state if state is not None else {
            "status": ["drafts_ok", "published"], "pr_number": 7,
            "pr_ref": "https://github.com/o/r/pull/7",
            "expected_targets": ["api-reference"],
            "payload": {"files": [{"path": "docs/api-reference.md",
                                   "target": "api-reference"}]},
        }
        self.resumes: list[dict] = []

    def get_state(self, cfg):
        return FakeSnapshot(self.state)

    def invoke(self, command, cfg):
        self.resumes.append(getattr(command, "resume", None))
        self.state = {**self.state, "status": ["revised_1", "published"],
                      "updated": True, "revisions_count": 1}
        return self.state


class FakeClient:
    def __init__(self, pr=None, comments=None, login="docagent-bot"):
        self.pr = pr if pr is not None else {
            "number": 7, "url": "https://github.com/o/r/pull/7", "state": "open",
            "merged": False, "draft": False, "title": "Docs"}
        self.comments = comments or []
        self.login = login
        self.posted: list[str] = []

    def find_pr(self, slug, branch): return self.pr

    def bot_login(self): return self.login

    def list_comments(self, slug, number): return list(self.comments)

    def comment(self, slug, number, body):
        self.posted.append(body)
        return {"id": 999, "url": "u"}


def _settings(tmp_path_like: Path) -> Settings:
    return Settings(workdir=tmp_path_like)


def _comment(cid, body, author="lead", created="2026-10-07T10:00:00Z"):
    return {"id": cid, "kind": "issue", "author": author, "body": body,
            "created_at": created, "url": f"https://github.com/o/r/pull/7#issuecomment-{cid}"}


def _workdir(name: str) -> Path:
    """Существующий каталог без создания подкаталогов (ограничение песочницы DSH)."""
    WORK.mkdir(parents=True, exist_ok=True)
    return WORK


def test_comment_with_change_request_revises_same_pr():
    s = _settings(_workdir("w"))
    g, c = FakeGraph(), FakeClient(comments=[_comment(1, "добавь пример вызова")])
    res = watcher.process_once(thread_id="t1", branch="docagent/t1", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    kinds = [a["action"] for a in res.actions]
    assert kinds == ["classified", "replied", "revised"]
    assert g.resumes == [{"decision": CHANGES, "feedback": "добавь пример вызова",
                          "reviewer": "lead"}]
    assert len(c.posted) == 2, "ответ на комментарий + подтверждение обновления"
    assert "тот же PR" in c.posted[1] or "этот же PR" in c.posted[1]
    watcher.state_path(s, "t1").unlink(missing_ok=True)


def test_question_gets_answer_without_rewrite():
    s = _settings(_workdir("w"))
    g, c = FakeGraph(), FakeClient(comments=[_comment(2, "почему тут api-reference?")])
    res = watcher.process_once(thread_id="t2", branch="docagent/t2", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert [a["action"] for a in res.actions] == ["classified", "replied"]
    assert g.resumes == [], "вопрос не должен запускать цикл переписывания"
    assert len(c.posted) == 1
    watcher.state_path(s, "t2").unlink(missing_ok=True)


def test_objection_gets_explanation_without_rewrite():
    s = _settings(_workdir("w"))
    g, c = FakeGraph(), FakeClient(comments=[_comment(3, "это неверно")])
    watcher.process_once(thread_id="t3", branch="docagent/t3", graph=g,
                         client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert g.resumes == []
    assert "разберусь по существу" in c.posted[0]
    watcher.state_path(s, "t3").unlink(missing_ok=True)


def test_bot_comments_ignored():
    s = _settings(_workdir("w"))
    g = FakeGraph()
    c = FakeClient(comments=[_comment(4, "добавь пример", author="docagent-bot")])
    res = watcher.process_once(thread_id="t4", branch="docagent/t4", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert res.actions == []
    assert g.resumes == [] and c.posted == []
    watcher.state_path(s, "t4").unlink(missing_ok=True)


def test_processed_comment_is_not_handled_twice():
    """Регрессия на перезапуск watcher: один комментарий = один resume."""
    s = _settings(_workdir("w"))
    cmt = _comment(5, "поправь заголовок")
    g1 = FakeGraph()
    c1 = FakeClient(comments=[cmt])
    watcher.process_once(thread_id="t5", branch="docagent/t5", graph=g1,
                         client=c1, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert len(g1.resumes) == 1
    # «новый процесс» watcher'а с тем же файлом состояния
    g2, c2 = FakeGraph(), FakeClient(comments=[cmt])
    res2 = watcher.process_once(thread_id="t5", branch="docagent/t5", graph=g2,
                                client=c2, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert res2.actions == [] and g2.resumes == [] and c2.posted == []
    watcher.state_path(s, "t5").unlink(missing_ok=True)


def test_merged_pr_ends_watch():
    s = _settings(_workdir("w"))
    pr = {"number": 7, "url": "u", "state": "closed", "merged": True,
          "draft": False, "title": "Docs"}
    g, c = FakeGraph(), FakeClient(pr=pr, comments=[_comment(6, "добавь пример")])
    res = watcher.process_once(thread_id="t6", branch="docagent/t6", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert res.done and res.reason == "PR смержен лидом"
    assert g.resumes == [] and c.posted == []
    watcher.state_path(s, "t6").unlink(missing_ok=True)


def test_closed_pr_ends_watch():
    s = _settings(_workdir("w"))
    pr = {"number": 7, "url": "u", "state": "closed", "merged": False,
          "draft": False, "title": "Docs"}
    g, c = FakeGraph(), FakeClient(pr=pr)
    res = watcher.process_once(thread_id="t7", branch="docagent/t7", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7")
    assert res.done and res.reason == "PR закрыт без мержа"
    watcher.state_path(s, "t7").unlink(missing_ok=True)


def test_refs_accept_slug_hash_form():
    """CLI и watcher обмениваются ссылкой вида owner/repo#N — она обязана парситься."""
    from docagent.github_int.refs import repo_and_pr
    assert repo_and_pr("Reactivity512/python-demo-repo#7") == \
        ("Reactivity512/python-demo-repo", 7)
    assert repo_and_pr("https://github.com/o/r/pull/7") == ("o/r", 7)
    assert repo_and_pr("gold-005") == (None, None)


def test_watcher_uses_explicit_pr_number():
    """Номер из 'slug#N' — источник истины: по нему идёт работа с комментариями."""
    s = _settings(_workdir("w"))
    g = FakeGraph()
    calls: list[tuple] = []

    class TrackingClient(FakeClient):
        def list_comments(self, slug, number):
            calls.append(("list", slug, number))
            return super().list_comments(slug, number)

        def comment(self, slug, number, body):
            calls.append(("comment", slug, number))
            return super().comment(slug, number, body)

    c = TrackingClient(comments=[_comment(9, "почему так?")])
    res = watcher.process_once(thread_id="t9", branch="docagent/t9", graph=g,
                               client=c, s=s,
                               pr_ref="Reactivity512/python-demo-repo#7")
    assert [a["action"] for a in res.actions] == ["classified", "replied"]
    assert calls and all(call[2] == 7 for call in calls), calls
    assert all(call[1] == "Reactivity512/python-demo-repo" for call in calls), calls
    watcher.state_path(s, "t9").unlink(missing_ok=True)


def test_dry_run_changes_nothing():
    s = _settings(_workdir("w"))
    g, c = FakeGraph(), FakeClient(comments=[_comment(8, "добавь пример")])
    res = watcher.process_once(thread_id="t8", branch="docagent/t8", graph=g,
                               client=c, s=s, pr_ref="https://github.com/o/r/pull/7",
                               dry_run=True)
    assert [a["action"] for a in res.actions] == ["classified"]
    assert g.resumes == [] and c.posted == []
    assert not watcher.state_path(s, "t8").exists()


# ------------------------------------- интеграция с настоящим графом (revise -> publish)
class _GraphFakeGithub:
    """Фейк GitHub, работающий и как клиент графа, и как клиент watcher'а."""

    def __init__(self, comments):
        self.comments = list(comments)
        self.posted: list[str] = []
        self.prs: list[tuple] = []
        self.created: dict = {}
        self.commits: list[str] = []

    # read side (нода fetch_pr)
    def diff_text(self, slug, number):
        self.prs.append((slug, number))
        return json.loads((REPO / "data" / "gold" / "cases" / "gold-005.json")
                          .read_text(encoding="utf-8"))["code_change"]["diff"]

    def pull_meta(self, slug, number):
        return {"slug": slug, "number": number, "title": "feat: cached settings",
                "body": "", "author": "dev", "state": "open", "draft": False,
                "merged": False, "base": "main", "head": "feature/x",
                "head_sha": "abc", "changed_files": ["demopkg/settings.py"]}

    # write side (publish)
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
        self.commits.append(message)
        return f"commit{len(self.commits)}"

    def open_draft_pr(self, slug, branch, title, body, base="main", draft=False):
        self.created.update({"branch": branch, "draft": draft})
        return {"number": 7, "html_url": "https://github.com/o/r/pull/7"}

    # watcher side
    def find_pr(self, slug, branch):
        return {"number": 7, "url": "https://github.com/o/r/pull/7", "state": "open",
                "merged": False, "draft": False, "title": "Docs"}

    def bot_login(self):
        return "docagent-bot"

    def list_comments(self, slug, number):
        return list(self.comments)

    def comment(self, slug, number, body):
        self.posted.append(body)
        return {"id": 100 + len(self.posted), "url": "u"}


class _UpdatedGraph:
    """Обёртка над настоящим графом: запоминает состояние после resume.

    Нужна, потому что watcher читает `updated` из возвращённого состояния, а
    LangGraph отдаёт его только при повторной публикации в существующую ветку.
    """

    def __init__(self, graph):
        self.graph = graph
        self.last = {}

    def get_state(self, cfg):
        return self.graph.get_state(cfg)

    def invoke(self, command, cfg):
        out = self.graph.invoke(command, cfg)
        if isinstance(out, dict):
            self.last = out
        return out


def test_watcher_resumes_real_graph_and_updates_same_pr(monkeypatch):
    """Сквозная проверка шага 5 без сети: комментарий -> revise -> publish -> ответ."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from docagent.github_int import client as client_mod
    from docagent.orchestrator.graph import build_graph

    monkeypatch.setenv("DOCAGENT_PUBLISH_TARGET", "github")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    get_settings.cache_clear()
    s = get_settings()
    fake = _GraphFakeGithub(comments=[_comment(11, "добавь пример вызова list_items")])
    # клиент графа подменяем в его модуле: nodes/publisher импортируют его лениво
    monkeypatch.setattr(client_mod, "GitHubClient", lambda *a, **k: fake)

    db = WORK / "cp-watch.db"
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    tid = "twatch"
    try:
        graph = build_graph(SqliteSaver(conn))
        first = graph.invoke({"pr_ref": "https://github.com/o/r/pull/7",
                              "backend_name": "fake", "lang": "ru", "thread_id": tid},
                             {"configurable": {"thread_id": tid}})
        assert "__interrupt__" in first, "граф обязан ждать ревью"
        assert fake.created.get("draft") is False

        res = watcher.process_once(thread_id=tid, branch=f"docagent/{tid}",
                                   graph=_UpdatedGraph(graph), client=fake, s=s,
                                   pr_ref="https://github.com/o/r/pull/7")
    finally:
        conn.close()
        get_settings.cache_clear()
        db.unlink(missing_ok=True)
        watcher.state_path(s, tid).unlink(missing_ok=True)

    actions = [a["action"] for a in res.actions]
    assert actions == ["classified", "replied", "revised"], actions
    assert len(fake.commits) == 2, "второй коммит — правка по замечанию в тот же PR"
    assert fake.created["branch"] == f"docagent/{tid}"
    assert any("этот же PR" in t for t in fake.posted)
