"""Клиент GitHub (шаг 4): PAT-auth через PyGithub + гибрид для чтения кода.

Гибридный diff:
  1. meta/diff берём из REST (pull_request.get_files()) — быстро, без клона;
  2. если нужен полный контекст файлов (чтение кода агентом) — shallow-клон
     head-рефа в data/work/<repo>-pr<n> (git depth=1).

Токен: DOCAGENT_GITHUB_TOKEN или GITHUB_TOKEN (env). Никогда не хардкодится.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from github import Github, GithubException
from github.Requester import Requester  # noqa: F401  (типизация)

from ..config import Settings, get_settings


class GitHubClient:
    def __init__(self, token: str | None = None, settings: Settings | None = None):
        self.s = settings or get_settings()
        tok = token or self.s.github_token or os.environ.get("GITHUB_TOKEN", "")
        if not tok:
            raise RuntimeError(
                "GitHub token не задан: экспортируй GITHUB_TOKEN=... "
                "или DOCAGENT_GITHUB_TOKEN=...")
        base = self.s.github_api_base.rstrip("/")
        self.api = Github(tok, base_url=base) if base != "https://api.github.com" \
            else Github(tok)
        self.token = tok

    # ------------------------------------------------------------- read side
    def repo(self, slug: str):
        return self.api.get_repo(slug)

    def pr(self, slug: str, number: int):
        return self.repo(slug).get_pull(number)

    def diff_text(self, slug: str, number: int) -> str:
        """Unified diff PR'а одним HTTP-запросом (Accept: vnd.github.diff)."""
        requester = self.api._Github__requester  # type: ignore[attr-defined]
        _, owner, name = slug.partition("/")
        url = f"/repos/{owner}/{name}/pulls/{number}"
        status, rdata, _ = requester.requestJson(
            "GET", url, headers={"Accept": "application/vnd.github.diff"})
        if status != 200:
            raise GithubException(status, rdata, None)
        return rdata.decode("utf-8", errors="replace")

    def changed_files(self, slug: str, number: int) -> list[dict]:
        """Файлы PR (patch может отсутствовать для бинарников)."""
        pr = self.pr(slug, number)
        return [{"filename": f.filename, "status": f.status,
                 "additions": f.additions, "deletions": f.deletions,
                 "patch": getattr(f, "patch", None)} for f in pr.get_files()]

    def clone_head(self, slug: str, number: int) -> Path:
        """Shallow-клон head-рефа PR (для чтения полного контекста файлов)."""
        pr = self.pr(slug, number)
        dest = Path(self.s.workdir) / f"{slug.replace('/', '_')}-pr{number}"
        if dest.exists():
            subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1",
                            "origin", pr.head.sha], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(dest), "checkout", "--detach",
                            pr.head.sha], check=True, capture_output=True)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://x-access-token:{self.token}@github.com/{slug}.git"
        subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1",
                        "origin", pr.head.sha], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "--detach",
                        pr.head.sha], check=True, capture_output=True)
        # токен вычищаем из remote, чтобы не остался в .git/config
        subprocess.run(["git", "-C", str(dest), "remote", "set-url",
                        "origin", f"https://github.com/{slug}.git"],
                       check=True, capture_output=True)
        return dest

    # ------------------------------------------------------------ write side
    def create_blob_commit(self, slug: str, branch: str, base_ref: str,
                           files: list[dict], message: str) -> str:
        """Создаёт коммит с файлами на ветке (без локального клона). Возвращает sha."""
        repo = self.repo(slug)
        ref = repo.get_git_ref(f"heads/{branch}")
        try:
            ref.edit(repo.get_git_commit(base_ref).sha, force=True)
        except GithubException:
            pass  # ветка уже указывает куда надо
        tree_items = []
        for f in files:
            blob = repo.create_git_blob(f["content"], "base64" if f.get("b64")
                                        else "utf-8")
            tree_items.append({"path": f["path"], "mode": "100644",
                               "type": "blob", "sha": blob.sha})
        base_commit = repo.get_git_commit(base_ref)
        tree = repo.create_git_tree(tree_items, base_commit.tree)
        commit = repo.create_git_commit(message, tree, [base_commit])
        ref.edit(commit.sha, force=True)
        return commit.sha

    def open_draft_pr(self, slug: str, branch: str, title: str, body: str,
                      base: str = "main") -> dict:
        repo = self.repo(slug)
        pr = repo.create_pull(title=title, body=body, head=branch, base=base,
                              draft=True)
        return {"number": pr.number, "html_url": pr.html_url}

    def whoami(self) -> str:
        return self.api.get_user().login
