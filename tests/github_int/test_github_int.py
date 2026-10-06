"""Unit-тесты github_int (шаг 4) без сети: fake-клиент + чистые функции."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.github_int.bootstrap import FILES, encode, plan_bootstrap  # noqa: E402
from docagent.github_int.publisher import _repo_slug, render_doc_file  # noqa: E402
from docagent.github_int.refs import parse_pr_ref  # noqa: E402


# ---------------------------------------------------------------- slug parsing
@pytest.mark.parametrize("pr_ref,expected", [
    ("https://github.com/foo/bar/pull/7", "foo/bar"),
    ("foo/bar#7", "foo/bar"),
    ("gold-005", "Reactivity512/python-demo-repo"),
    ("/tmp/x/y.diff", "Reactivity512/python-demo-repo"),
])
def test_repo_slug(pr_ref, expected):
    class S:
        demo_repo = "Reactivity512/python-demo-repo"
    assert _repo_slug({"pr_ref": pr_ref}, S()) == expected


@pytest.mark.parametrize("pr_ref,expected", [
    ("https://github.com/o/r/pull/7", ("o/r", 7)),
    ("https://github.com/o/r/pulls/12", ("o/r", 12)),
    ("https://github.com/o/r/pull/7/files#diff-abc", ("o/r", 7)),
    ("https://github.com/o/r/pull/7/", ("o/r", 7)),
    ("o/r#7", ("o/r", 7)),
    ("o/r", ("o/r", None)),
    ("o/r.git", ("o/r", None)),
    ("o/r/tree/main", ("o/r", None)),
    ("https://github.com/o/r", ("o/r", None)),
    ("git@github.com:o/r.git", ("o/r", None)),
    # не ссылки на PR: номер не выдумываем
    ("gold-005", (None, None)),
    ("data/work/x.diff", (None, None)),
    ("F:\\!prog_new\\x-pr7.diff", (None, None)),
    ("/tmp/x/y.diff", (None, None)),
    ("https://gitlab.com/o/r/pull/7", (None, None)),
    ("", (None, None)),
])
def test_parse_pr_ref(pr_ref, expected):
    assert parse_pr_ref(pr_ref) == expected


# ---------------------------------------------------------------- render
def test_render_creates_skeleton():
    out = render_doc_file(None, {"path": "docs/api.md", "target": "api-reference",
                                 "content_md": "### Client.list_items\nbody"})
    assert out.startswith("# Api Reference")
    assert "DocAgent · api-reference" in out
    assert "Client.list_items" in out


def test_render_replaces_previous_block_not_others():
    existing = ("# API\n\n## Intro\n\nkeep me\n\n"
                "## DocAgent · api-reference\n\nOLD TEXT\n")
    out = render_doc_file(existing, {"path": "docs/api.md",
                                     "target": "api-reference",
                                     "content_md": "NEW TEXT"})
    assert "OLD TEXT" not in out
    assert "NEW TEXT" in out
    assert "keep me" in out  # чужие секции не трогаем


def test_render_appends_when_no_block():
    existing = "# API\n\n## Intro\n\nhello\n"
    out = render_doc_file(existing, {"path": "docs/api.md",
                                     "target": "readme", "content_md": "added"})
    assert out.startswith("# API")
    assert "## DocAgent · readme" in out and "added" in out


# ---------------------------------------------------------------- bootstrap plan
class FakeContentErr(Exception):
    def __init__(self, status): self.status = status


class FakeRepo:
    """README уже есть в репо, остального нет."""
    def get_contents(self, path):
        from github import GithubException
        if path == "README.md":
            return object()
        raise GithubException(404, {}, None)


class FakeClient:
    def repo(self, slug): return FakeRepo()


def test_bootstrap_plan_skips_existing():
    plan = plan_bootstrap(FakeClient(), "foo/bar")
    paths = {p["path"] for p in plan}
    assert "README.md" not in paths          # не перезаписываем существующее
    assert "demopkg/client.py" in paths
    assert len(plan) == len(FILES) - 1
    # контент обязан быть base64 для Git Data API
    import base64
    for p in plan:
        assert base64.b64decode(p["content"]).decode() == FILES[p["path"]]


def test_bootstrap_missing_files_roundtrip():
    assert encode(FILES["pyproject.toml"]) != FILES["pyproject.toml"]


# ---------------------------------------------------------------- publish via fake client
class FakePR:
    number = 42
    html_url = "https://github.com/foo/bar/pull/42"


class FakePublishRepo:
    def __init__(self):
        self.branch_sha = "deadbeef"
        self.created_branches = []
        self.prs = []

    def get_contents(self, path, ref=None):
        from github import GithubException
        raise GithubException(404, {}, None)

    def get_branch(self, name):
        class B: commit = type("C", (), {"sha": self.branch_sha})()
        return B()

    def get_git_ref(self, ref):
        from github import GithubException
        raise GithubException(404, {}, None)  # ветки нет — первый publish

    def get_pulls(self, state="open", head=None):
        return []

    def get_git_commit(self, sha):
        class T: pass
        class C:
            tree = T()
            sha = sha
        return C()

    def create_git_blob(self, content, encoding):
        class Blob: sha = "blobsha"
        return Blob()

    def create_git_tree(self, items, base_tree):
        class Tree: pass
        return Tree()

    def create_git_commit(self, message, tree, parents):
        class Commit: sha = "newcommit"
        return Commit()

    def create_pull(self, **kw):
        self.prs.append(kw)
        return FakePR()


class FakePublishClient:
    def __init__(self): self.repo_obj = FakePublishRepo()
    def repo(self, slug): return self.repo_obj
    def create_blob_commit(self, slug, branch, base_ref, files, message):
        return "newcommit"
    def open_draft_pr(self, slug, branch, title, body, base="main", draft=False):
        self.repo_obj.prs.append({"title": title, "body": body, "head": branch,
                                  "base": base, "draft": draft})
        return {"number": 42, "html_url": "https://github.com/foo/bar/pull/42"}


def test_publish_to_github_flow(monkeypatch):
    from docagent.config import Settings
    from docagent.github_int import publisher

    s = Settings(publish_target="github")
    state = {
        "thread_id": "pr-demo-1",
        "payload": {
            "pr_ref": "https://github.com/foo/bar/pull/1",
            "commit_message": "docs: auto",
            "body_md": "## body",
            "files": [{"path": "docs/api-reference.md", "target": "api-reference",
                       "content_md": "text about Client", "action": "append"}],
        },
    }
    fc = FakePublishClient()
    res = publisher.publish_to_github(state, s, client=fc)
    assert res == {"pr_number": 42,
                   "pr_url": "https://github.com/foo/bar/pull/42",
                   "publish_mode": "github"}
    kw = fc.repo_obj.prs[0]
    assert kw["draft"] is False                       # решение шага 5: PR сразу готов к ревью
    assert kw["head"] == "docagent/pr-demo-1"        # идемпотентная ветка по thread
    assert kw["base"] == "main"
    assert "Docs:" in kw["title"]


def test_publish_empty_diff_raises(monkeypatch):
    """Регрессия на 'No commits between main and <branch>': если контент
    черновика уже в файле — publish обязан упасть с внятной ошибкой, а не
    создавать пустой PR."""
    from docagent.config import Settings
    from docagent.github_int import publisher

    class ExistingContentRepo(FakePublishRepo):
        """main уже содержит точно такой же блок -> files_out пуст."""
        def get_contents(self, path, ref=None):
            existing_main = (
                "# Api Reference\n\n"
                "_Сгенерировано DocAgent (черновик, до одобрения team lead)._ \n\n"
                "## DocAgent · api-reference\n\ntext about Client\n")
            return type("R", (), {"decoded_content": existing_main.encode()})()

    class C:
        def repo(self, slug): return ExistingContentRepo()
        def create_blob_commit(self, *a, **k): raise AssertionError("must not commit")
        # ветки нет (FakePublishRepo.get_git_ref -> 404), контент совпадает с main
        def open_draft_pr(self, *a, **k): raise AssertionError("must not open PR")

    s = Settings(publish_target="github")
    state = {
        "thread_id": "pr-demo-1",
        "payload": {
            "pr_ref": "https://github.com/foo/bar/pull/1",
            "files": [{"path": "docs/api-reference.md", "target": "api-reference",
                       "content_md": "text about Client", "action": "append"}],
        },
    }
    import pytest
    with pytest.raises(RuntimeError, match="nothing to commit"):
        publisher.publish_to_github(state, s, client=C())


def test_publish_updates_existing_branch_pr():
    """Регрессия на 'No commits between main and <branch>' при ревью-правках:
    если ветка треда уже существует — коммит поверх её HEAD (не поверх main),
    PR обновляется, дубль не создаётся."""
    from docagent.config import Settings
    from docagent.github_int import publisher

    class BranchExistsRepo(FakePublishRepo):
        def get_git_ref(self, ref):
            o = type("O", (), {"sha": "branchhead123"})()
            return type("R", (), {"object": o})()
        def get_pulls(self, state="open", head=None):
            pr = type("P", (), {"number": 42, "html_url": "https://github.com/foo/bar/pull/42",
                                "edit": lambda self, **kw: None})()
            self.edited = kw if False else None
            return [pr]

    captured = {}
    class C(FakePublishClient):
        def __init__(self):
            self.repo_obj = BranchExistsRepo()
        def create_blob_commit(self, slug, branch, base_ref, files, message):
            captured["parent"] = base_ref
            captured["files"] = files
            return "newcommit"
        def open_draft_pr(self, *a, **k):
            raise AssertionError("must not open duplicate PR")

    s = Settings(publish_target="github")
    state = {
        "thread_id": "pr-demo-1",
        "payload": {
            "pr_ref": "https://github.com/foo/bar/pull/1",
            "commit_message": "docs: v2 after review",
            "body_md": "## body v2",
            "files": [{"path": "docs/api-reference.md", "target": "api-reference",
                       "content_md": "UPDATED text about Client", "action": "append"}],
        },
    }
    res = publisher.publish_to_github(state, s, client=C())
    assert captured["parent"] == "branchhead123"   # parent = HEAD ветки, не main
    assert res["updated"] is True and res["pr_number"] == 42


def test_publish_escalation_stays_draft():
    """Эскалация needs_human_edit — исключение: PR публикуется как draft,
    чтобы неготовый черновик не выглядел готовым к мержу."""
    from docagent.config import Settings
    from docagent.github_int import publisher

    s = Settings(publish_target="github")
    state = {
        "thread_id": "pr-escalated",
        "payload": {
            "pr_ref": "https://github.com/foo/bar/pull/1",
            "commit_message": "docs: auto",
            "body_md": "## body",
            "needs_human_edit": True,
            "files": [{"path": "docs/api-reference.md", "target": "api-reference",
                       "content_md": "text about Client", "action": "append"}],
        },
    }
    fc = FakePublishClient()
    res = publisher.publish_to_github(state, s, client=fc)
    assert res["pr_number"] == 42
    assert fc.repo_obj.prs[0]["draft"] is True


def test_repo_slug_windows_path_falls_back_to_demo():
    """F:\\...\\file.diff — это путь, не owner/repo: fallback на demo_repo."""
    from docagent.config import get_settings
    from docagent.github_int.publisher import _repo_slug
    s = get_settings()
    assert _repo_slug({"pr_ref": "F:\\!prog_new\\x-pr7.diff"}, s) == s.demo_repo
    assert _repo_slug({"pr_ref": "/tmp/x/y.diff"}, s) == s.demo_repo
    # реальные slug'ы парсятся как раньше
    assert _repo_slug({"pr_ref": "https://github.com/o/r/pull/7"}, s) == "o/r"
    assert _repo_slug({"pr_ref": "o/r#7"}, s) == "o/r"


def test_ensure_branch_at_creates_and_resets(fake_gh=None):
    """ensure_branch_at: 404 -> create_git_ref, иначе edit(force)."""
    import types
    from github import GithubException

    from docagent.github_int.client import GitHubClient

    calls = []

    class Ref:
        object = types.SimpleNamespace(sha="aaa")
        def edit(self, sha, force=False): calls.append(("edit", sha, force))

    class RepoExists:
        def get_git_ref(self, path): return Ref()

    class RepoMissing:
        def get_git_ref(self, path): raise GithubException(404, {}, None)
        def create_git_ref(self, ref, sha): calls.append(("create", ref, sha))

    c = GitHubClient.__new__(GitHubClient)  # без токена: тест только логику ref'а
    c.repo = lambda slug: RepoExists()
    GitHubClient.ensure_branch_at(c, "o/r", "b", "bbb")
    assert calls[-1] == ("edit", "bbb", True)
    c.repo = lambda slug: RepoMissing()
    GitHubClient.ensure_branch_at(c, "o/r", "b", "ccc")
    assert calls[-1] == ("create", "refs/heads/b", "ccc")
