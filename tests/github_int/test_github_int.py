"""Unit-тесты github_int (шаг 4) без сети: fake-клиент + чистые функции."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.github_int.bootstrap import FILES, encode, plan_bootstrap  # noqa: E402
from docagent.github_int.publisher import _repo_slug, render_doc_file  # noqa: E402


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
    def get_content(self, path):
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

    def get_content(self, path):
        from github import GithubException
        raise GithubException(404, {}, None)

    def get_branch(self, name):
        class B: commit = type("C", (), {"sha": self.branch_sha})()
        return B()

    def get_git_ref(self, ref): 
        class R:
            def edit(_self, sha, force=False): pass
        return R()

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
    def open_draft_pr(self, slug, branch, title, body, base="main"):
        self.repo_obj.prs.append({"title": title, "body": body, "head": branch,
                                  "base": base, "draft": True})
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
    assert kw["draft"] is True                      # только draft PR
    assert kw["head"] == "docagent/pr-demo-1"        # идемпотентная ветка по thread
    assert "Docs:" in kw["title"]
