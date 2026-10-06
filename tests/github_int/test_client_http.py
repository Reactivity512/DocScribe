"""Тесты клиента GitHub на уровне HTTP (шаги 4-5): mock GitHub REST API.

Зачем отдельный слой: подмена `GitHubClient` фейком проверяет логику графа, но НЕ
проверяет вызовы PyGithub. Именно там уже дважды ломались вещи, которые видны
только на живом API: dict вместо `InputGitTreeElement` в `create_git_tree` и
несуществующий `get_content()`. Здесь клиент говорит с настоящим HTTP-сервером,
который отвечает как GitHub, и проверяется и результат, и тело запросов.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.config import Settings  # noqa: E402
from docagent.github_int.client import GitHubClient  # noqa: E402

DIFF = "diff --git a/demopkg/settings.py b/demopkg/settings.py\n+def get_cached(key):\n"


class _State:
    """Состояние «GitHub»: ветки, PR-ы, комментарии, записанные запросы."""

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.requests: list[tuple[str, str, dict]] = []
        self.headers: list[dict] = []
        self.branches: dict[str, dict] = {}          # ref -> {sha, parents}
        self.pr: dict | None = None
        self.issue_comments: list[dict] = []
        self.review_comments: list[dict] = []
        self.posted_comments: list[str] = []
        self.reviews: list[dict] = []
        self.blobs: list[dict] = []
        self.merged = False
        self.pr_state = "open"
        # parents по sha: нужны, чтобы отличить наш коммит-бранч от sha main
        self.commit_parents: dict[str, list[str]] = {"basesha": ["root"],
                                                     "branchhead": ["older"]}
        self.commit_shas = iter([f"commit{i}" for i in range(1, 20)])

    def add_branch(self, name: str, sha: str, parents: list[str] | None = None) -> None:
        self.branches[f"refs/heads/{name}"] = {"sha": sha, "parents": parents or []}


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # тишина в выводе тестов
            pass

        def handle_one_request(self):
            """В debug-режиме печатаем причину: иначе сервер молча рвёт соединение."""
            try:
                super().handle_one_request()
            except Exception:  # noqa: BLE001
                if state.debug:
                    self._send(500, {"message": "mock handler error"})
                raise

        def _send(self, code: int, payload) -> None:
            body = payload if isinstance(payload, bytes) else \
                json.dumps(payload).encode("utf-8")
            if state.debug:
                print(f"      -> {code} ({len(body)}b)", flush=True)
            # один ответ на одно соединение: иначе urllib3 переиспользует keep-alive
            # соединение, которое BaseHTTPRequestHandler закрывает -> RemoteDisconnected
            self.close_connection = True
            self.send_response(code)
            media = "application/vnd.github.diff" \
                if isinstance(payload, bytes) else "application/json"
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                return {}

        def _record(self, method: str, body: dict) -> str:
            path = self.path
            state.requests.append((method, path, body))
            state.headers.append(dict(self.headers))
            if state.debug:
                print(f"MOCK {method:6} {path}", flush=True)
            return path

        # ------------------------------------------------------------- GET --
        def do_GET(self):
            path = self._record("GET", {})
            accept = self.headers.get("Accept", "")
            if path == "/user":
                return self._send(200, {"login": "docagent-bot", "id": 1})
            if path.startswith("/repos/o/r/pulls/7") and "diff" in accept:
                return self._send(200, DIFF.encode("utf-8"))
            if path.startswith("/repos/o/r/pulls/7/comments"):
                return self._send(200, state.review_comments)
            if path.startswith("/repos/o/r/pulls/7/files"):
                return self._send(200, [{"filename": "demopkg/settings.py",
                                         "status": "modified", "additions": 5,
                                         "deletions": 0,
                                         "patch": "@@ +def get_cached"}])
            if path.startswith("/repos/o/r/pulls/7"):
                return self._send(200, self._pr_payload())
            if path.startswith("/repos/o/r/pulls?"):
                return self._send(200, [self._pr_payload()] if state.pr else [])
            if path.startswith("/repos/o/r/issues/7/comments"):
                return self._send(200, state.issue_comments)
            if path.startswith("/repos/o/r/git/ref/"):
                ref = path.split("/repos/o/r/git/ref/", 1)[1]
                # PyGithub спрашивает и "heads/x", и "refs/heads/x"
                info = state.branches.get(ref) or state.branches.get(f"refs/{ref}")
                if info is None:
                    return self._send(404, {"message": "Not Found"})
                full = ref if ref.startswith("refs/") else f"refs/{ref}"
                return self._send(200, {"ref": full,
                                        "object": {"sha": info["sha"],
                                                   "type": "commit"}})
            if path.startswith("/repos/o/r/git/commits/"):
                sha = path.rsplit("/", 1)[1]
                parents = state.commit_parents.get(sha, [])
                # важно: родителей отдаём всегда (даже пустой список), иначе
                # PyGithub не создаёт атрибут parents и логика ветки ломается
                return self._send(200, {"sha": sha, "tree": {"sha": "tree-base"},
                                        "parents": [{"sha": p} for p in parents]})
            if path.startswith("/repos/o/r/branches/"):
                name = path.rsplit("/", 1)[1]
                info = state.branches.get(f"refs/heads/{name}")
                if info is None:
                    return self._send(404, {"message": "Branch not found"})
                return self._send(200, {"name": name,
                                        "commit": {"sha": info["sha"]}})
            if path == "/repos/o/r":
                return self._send(200, {"full_name": "o/r",
                                        "default_branch": "main"})
            return self._send(404, {"message": f"unmocked GET {path}"})

        def _pr_payload(self) -> dict:
            # url обязателен: PyGithub строит из него issue_url для комментариев
            url = f"{self.server._gh_base}/repos/o/r/pulls/7"
            return {
                "number": 7, "title": "feat: cached settings", "body": "тело PR",
                "state": state.pr_state, "draft": False, "merged": state.merged,
                "html_url": "https://github.com/o/r/pull/7",
                "url": url, "issue_url": f"{self.server._gh_base}/repos/o/r/issues/7",
                "comments_url": f"{url}/comments",
                "user": {"login": "dev"},
                "head": {"ref": "feature/x", "sha": "headsha"},
                "base": {"ref": "main", "sha": "basesha"},
            }

        # ------------------------------------------------------------ POST --
        def do_POST(self):
            body = self._read_body()
            try:
                self._do_POST(body)
            except Exception as e:  # noqa: BLE001
                # отдаём причину клиенту: иначе соединение рвётся и видно только
                # RemoteDisconnected без объяснения
                import traceback
                self._send(500, {"message": f"mock error: {type(e).__name__}: {e}",
                                 "trace": traceback.format_exc()[-800:]})

        def _do_POST(self, body: dict):
            path = self._record("POST", body)
            if path == "/repos/o/r/git/blobs":
                encoding = body.get("encoding", "utf-8")
                if encoding == "base64":
                    # GitHub принимает base64 без паддинга: дополняем сами
                    raw = body["content"]
                    raw += "=" * (-len(raw) % 4)
                    content = base64.b64decode(raw).decode("utf-8")
                else:
                    content = body["content"]
                state.blobs.append({"content": content, "encoding": encoding})
                # как настоящий GitHub: sha блоба — hex
                return self._send(201, {"sha": hashlib.sha1(
                    content.encode()).hexdigest()})
            if path == "/repos/o/r/git/trees":
                tree = body.get("tree") or []
                assert tree and all(isinstance(t, dict) for t in tree), tree
                assert all({"path", "mode", "type"} <= set(t) for t in tree), tree
                return self._send(201, {"sha": "newtree"})
            if path == "/repos/o/r/git/commits":
                sha = next(state.commit_shas)
                # PyGithub шлёт parents как список строк sha, а не объектов
                raw_parents = body.get("parents") or []
                parents = [p if isinstance(p, str) else p.get("sha", "")
                           for p in raw_parents]
                state.commit_parents[sha] = parents
                return self._send(201, {"sha": sha, "tree": {"sha": body["tree"]},
                                        "parents": [{"sha": p} for p in parents]})
            if path == "/repos/o/r/git/refs":
                state.branches[body["ref"]] = {"sha": body["sha"],
                                               "parents": ["base"]}
                return self._send(201, {"ref": body["ref"],
                                        "object": {"sha": body["sha"]}})
            if path == "/repos/o/r/pulls":
                state.pr = {"number": 7, "head": body["head"]}
                return self._send(201, self._pr_payload())
            if path == "/repos/o/r/issues/7/comments":
                state.posted_comments.append(body["body"])
                return self._send(201, {"id": 500 + len(state.posted_comments),
                                        "body": body["body"], "html_url": "u",
                                        "user": {"login": "docagent-bot"}})
            if path == "/repos/o/r/pulls/7/reviews":
                state.reviews.append(body)
                return self._send(201, {"id": 1, "state": body.get("event")})
            return self._send(404, {"message": f"unmocked POST {path}"})

        # ----------------------------------------------------------- PATCH --
        def do_PATCH(self):
            body = self._read_body()
            path = self._record("PATCH", body)
            # PyGithub патчит /git/ref/<ref> (без "s"), а создаёт через /git/refs
            for marker in ("/git/refs/", "/git/ref/"):
                if marker in path:
                    ref = path.split(marker, 1)[1]
                    if not ref.startswith("refs/"):
                        ref = f"refs/{ref}"
                    state.branches[ref] = {"sha": body["sha"], "parents": ["base"]}
                    return self._send(200, {"ref": ref,
                                            "object": {"sha": body["sha"]}})
            return self._send(404, {"message": f"unmocked PATCH {path}"})

    return Handler


@pytest.fixture()
def gh():
    """Поднятый mock-GitHub + клиент, направленный на него."""
    state = _State(debug=bool(os.environ.get("MOCK_DEBUG")))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    server._gh_base = f"http://127.0.0.1:{server.server_address[1]}"  # для payload-URL
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = GitHubClient(settings=Settings(github_api_base=server._gh_base,
                                            github_token="test-token"))
    try:
        yield client, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assert_requests(state: _State, needle: str, method: str | None = None
                     ) -> list[tuple[str, str, dict]]:
    """Запросы по подстроке пути (и методу: GET/POST на один путь различаются)."""
    return [r for r in state.requests
            if needle in r[1] and (method is None or r[0] == method)]


# ------------------------------------------------------------------ read side --
def test_whoami_and_diff_text_over_http(gh):
    """Регрессия: diff_text строил URL '/repos///r/pulls/N' (404)."""
    client, state = gh
    assert client.whoami() == "docagent-bot"
    assert client.diff_text("o/r", 7).startswith("diff --git")
    diff_paths = [r[1] for r in state.requests if r[1].endswith("/pulls/7")]
    assert diff_paths == ["/repos/o/r/pulls/7"], diff_paths
    accepts = {h.get("Accept", "") for h in state.headers}
    assert "application/vnd.github.diff" in accepts
    auths = {h.get("Authorization", "") for h in state.headers}
    # PyGithub шлёт "token <pat>", наш прямой diff-запрос — "Bearer <pat>"
    assert "Bearer test-token" in auths, auths


def test_diff_text_error_is_github_exception(gh):
    from github import GithubException

    client, _state = gh
    with pytest.raises(GithubException) as e:
        client.diff_text("o/r", 404)
    assert e.value.status == 404


def test_diff_text_rejects_bad_slug(gh):
    client, _state = gh
    with pytest.raises(ValueError):
        client.diff_text("just-name", 7)


def test_pull_meta_reads_pr_fields(gh):
    client, _state = gh
    meta = client.pull_meta("o/r", 7)
    assert meta["slug"] == "o/r" and meta["number"] == 7
    assert meta["title"] == "feat: cached settings"
    assert meta["base"] == "main" and meta["head"] == "feature/x"
    assert meta["head_sha"] == "headsha" and meta["author"] == "dev"
    assert meta["changed_files"] == ["demopkg/settings.py"]
    assert meta["draft"] is False and meta["merged"] is False


def test_changed_files_includes_patch(gh):
    client, _state = gh
    files = client.changed_files("o/r", 7)
    assert files[0]["filename"] == "demopkg/settings.py"
    assert files[0]["patch"].startswith("@@ +def get_cached")


def test_missing_branch_raises_404(gh):
    from github import GithubException

    client, _state = gh
    with pytest.raises(GithubException) as e:
        client.repo("o/r").get_git_ref("heads/nope")
    assert e.value.status == 404


# ----------------------------------------------------------------- write side --
def test_create_blob_commit_creates_branch_and_payload_is_valid(gh):
    """Первый коммит: ветки нет -> create_git_ref, тело дерева корректное."""
    client, state = gh
    state.branches["refs/heads/main"] = {"sha": "basesha", "parents": ["root"]}
    sha = client.create_blob_commit(
        "o/r", "docagent/t1", "basesha",
        [{"path": "docs/api-reference.md", "content": "# Api\n\ntext\n"}],
        "docs: автообновление")
    assert sha.startswith("commit")

    blobs = _assert_requests(state, "/git/blobs", "POST")
    assert len(blobs) == 1
    # для utf-8 контент уходит как есть (не base64) — так и ждёт GitHub Git Data API
    assert blobs[0][2]["content"] == "# Api\n\ntext\n"
    assert blobs[0][2]["encoding"] == "utf-8"
    assert state.blobs[0]["content"] == "# Api\n\ntext\n"

    trees = _assert_requests(state, "/git/trees", "POST")
    assert trees[0][2]["tree"][0]["path"] == "docs/api-reference.md"
    assert trees[0][2]["tree"][0]["mode"] == "100644"
    assert trees[0][2]["tree"][0]["type"] == "blob"
    assert trees[0][2]["base_tree"] == "tree-base"

    commits = _assert_requests(state, "/git/commits", "POST")
    assert commits[0][2]["message"] == "docs: автообновление"
    assert commits[0][2]["parents"] == ["basesha"]

    refs = _assert_requests(state, "/git/refs", "POST")
    assert refs[0][2]["ref"] == "refs/heads/docagent/t1"
    assert state.branches["refs/heads/docagent/t1"]["sha"] == sha


def test_create_blob_commit_updates_existing_branch_head(gh):
    """Повторный прогон/правка: коммит кладётся поверх HEAD ветки, не поверх main."""
    client, state = gh
    state.add_branch("docagent/t2", "branchhead", parents=["older"])
    state.branches["refs/heads/main"] = {"sha": "basesha", "parents": ["root"]}
    client.create_blob_commit("o/r", "docagent/t2", "basesha",
                              [{"path": "docs/x.md", "content": "v2\n"}], "docs: v2")
    commits = _assert_requests(state, "/git/commits", "POST")
    assert commits[0][2]["parents"] == ["branchhead"]
    assert not _assert_requests(state, "/git/refs", "POST"), \
        "ветка уже есть — создавать заново не нужно"
    patches = _assert_requests(state, "git/ref/heads/docagent/t2", "PATCH")
    assert patches and patches[0][0] == "PATCH"
    assert patches[0][2]["force"] is True


def test_create_blob_commit_base64_for_bootstrap(gh):
    client, state = gh
    state.branches["refs/heads/main"] = {"sha": "basesha", "parents": ["root"]}
    payload = base64.b64encode("README\n".encode()).decode()
    client.create_blob_commit("o/r", "docagent/t3", "basesha",
                              [{"path": "README.md", "content": payload,
                                "b64": True}],
                              "chore: bootstrap")
    blobs = _assert_requests(state, "/git/blobs", "POST")
    assert blobs[0][2]["encoding"] == "base64"
    assert blobs[0][2]["content"] == payload
    assert state.blobs[0]["content"] == "README\n"


def test_open_pr_is_not_draft_by_default(gh):
    client, state = gh
    res = client.open_draft_pr("o/r", "docagent/t4", "Docs: o/r#7", "body")
    assert res["number"] == 7 and res["html_url"].endswith("/pull/7")
    created = [r for r in state.requests if r[1] == "/repos/o/r/pulls"][-1]
    assert created[2]["draft"] is False
    assert created[2]["head"] == "docagent/t4" and created[2]["base"] == "main"


def test_open_pr_draft_for_escalation(gh):
    client, state = gh
    client.open_draft_pr("o/r", "docagent/t5", "Docs", "body", draft=True)
    created = [r for r in state.requests if r[1] == "/repos/o/r/pulls"][-1]
    assert created[2]["draft"] is True


# ---------------------------------------------------------------- watcher side --
def test_find_pr_returns_state_and_merge_flag(gh):
    client, state = gh
    state.pr = {"number": 7}
    found = client.find_pr("o/r", "docagent/t6")
    assert found["number"] == 7 and found["state"] == "open"
    assert found["merged"] is False
    head_filters = [r for r in state.requests if "/pulls?" in r[1]]
    assert head_filters, "поиск PR обязан фильтровать по head=owner:branch"


def test_list_comments_merges_issue_and_review(gh):
    client, state = gh
    state.issue_comments = [{"id": 1, "body": "добавь пример", "html_url": "u1",
                             "created_at": "2026-10-07T10:00:00Z",
                             "user": {"login": "lead"}}]
    state.review_comments = [{"id": 2, "body": "почему так?", "html_url": "u2",
                              "created_at": "2026-10-07T11:00:00Z",
                              "user": {"login": "dev"}}]
    comments = client.list_comments("o/r", 7)
    assert [c["id"] for c in comments] == [1, 2]
    assert {c["kind"] for c in comments} == {"issue", "review"}
    assert comments[0]["author"] == "lead"


def test_comment_and_review_ready(gh):
    client, state = gh
    res = client.comment("o/r", 7, "принял: переписываю")
    assert res["id"] == 501
    assert state.posted_comments == ["принял: переписываю"]
    client.review_ready("o/r", 7)
    assert state.reviews and state.reviews[0]["event"] == "APPROVE"
