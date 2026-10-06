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
import urllib.error
import urllib.request
from pathlib import Path

from github import Auth, Github, GithubException, InputGitTreeElement
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
        auth = Auth.Token(tok)  # github.Auth: старый login_or_token даёт DeprecationWarning
        self.api = Github(auth=auth, base_url=base) if base != "https://api.github.com" \
            else Github(auth=auth)
        self.token = tok

    # ------------------------------------------------------------- read side
    def repo(self, slug: str):
        return self.api.get_repo(slug)

    def pr(self, slug: str, number: int):
        return self.repo(slug).get_pull(number)

    def diff_text(self, slug: str, number: int) -> str:
        """Unified diff PR'а одним HTTP-запросом (Accept: vnd.github.diff).

        Ходим напрямую через urllib, а не через внутренний `requestJson` PyGithub:
        тот возвращает тело то строкой, то разобранным JSON/dict (в зависимости от
        Content-Type) и при сбое чтения silently повторяет запрос — на diff-эндпоинте
        это давало вместо diff-а JSON-ошибку сервера. Здесь контракт один: 200 ->
        текст diff-а, иначе GithubException.
        """
        owner, _, name = slug.partition("/")
        if not owner or not name:
            raise ValueError(f"diff_text: ожидается 'owner/repo', получено {slug!r}")
        url = f"{self.s.github_api_base.rstrip('/')}/repos/{owner}/{name}/pulls/{number}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github.diff",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "docagent",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.s.llm_timeout_s) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise GithubException(e.code, detail, None) from e

    def changed_files(self, slug: str, number: int) -> list[dict]:
        """Файлы PR (patch может отсутствовать для бинарников)."""
        pr = self.pr(slug, number)
        return [{"filename": f.filename, "status": f.status,
                 "additions": f.additions, "deletions": f.deletions,
                 "patch": getattr(f, "patch", None)} for f in pr.get_files()]

    def pull_meta(self, slug: str, number: int) -> dict:
        """Метаданные PR одним объектом — для ноды fetch_pr (шаг 4).

        Один запрос get_pull + get_files; тело PR нужно промпту, слаг/номер —
        ветке публикации (шаг 5), поэтому отдаём их вместе с diff-ом.
        """
        pr = self.pr(slug, number)
        return {
            "slug": slug,
            "number": number,
            "title": pr.title,
            "body": pr.body or "",
            "author": pr.user.login if pr.user else "",
            "state": pr.state,
            "draft": bool(pr.draft),
            "merged": bool(pr.merged),
            "base": pr.base.ref,
            "head": pr.head.ref,
            "head_sha": pr.head.sha,
            "changed_files": [f.filename for f in pr.get_files()],
        }

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
    def ensure_branch_at(self, slug: str, branch: str, sha: str) -> None:
        """Создаёт ветку branch на sha или сбрасывает её на него (force).

        Гарантирует: base и head не равны => GitHub не выдаст
        'No commits between main and <branch>'.
        """
        repo = self.repo(slug)
        try:
            ref = repo.get_git_ref(f"heads/{branch}")
            if ref.object.sha != sha:
                ref.edit(sha, force=True)
        except GithubException as e:
            if e.status != 404:
                raise
            repo.create_git_ref(f"refs/heads/{branch}", sha)

    def create_blob_commit(self, slug: str, branch: str, base_ref: str,
                           files: list[dict], message: str) -> str:
        """Создаёт коммит с файлами на ветке (без локального клона). Возвращает sha.

        Если ветка уже существует — коммит кладётся поверх её HEAD (иначе
        перезаписанный docs-файл дал бы дерево, идентичное базе, и GitHub
        отклонил бы PR 'No commits between main and <branch>').
        Ветка создаётся только ПОСЛЕ успешного коммита — при пустом diff в репо
        не остаётся мусорной ветки, идентичной main.
        """
        repo = self.repo(slug)
        try:  # существующая ветка => пишем поверх неё (апдейт PR), иначе на базу
            cur = repo.get_git_ref(f"heads/{branch}").object.sha
            if repo.get_git_commit(cur).parents:  # это наш commit-branch, не main-sha
                base_ref = cur
        except GithubException:
            pass  # ветки нет — используем base_ref как есть
        tree_items = []
        for f in files:
            blob = repo.create_git_blob(f["content"], "base64" if f.get("b64")
                                        else "utf-8")
            # PyGithub требует объекты InputGitTreeElement, а не dict:
            # create_git_tree() ассертит тип каждого элемента и читает
            # element._identity (иначе AssertionError на plain dict).
            tree_items.append(InputGitTreeElement(path=f["path"], mode="100644",
                                                  type="blob", sha=blob.sha))
        base_commit = repo.get_git_commit(base_ref)
        try:
            tree = repo.create_git_tree(tree_items, base_commit.tree)
        except GithubException as e:
            raise RuntimeError(
                f"create_blob_commit {slug}: дерево идентично базе "
                f"{base_ref[:8]} (GitHub вернул {e.status}). PR с таким diff'ом "
                "невозможно — 'No commits between main and <branch>'.") from e
        commit = repo.create_git_commit(message, tree, [base_commit])
        # ветка создаётся/сбрасывается ТОЛЬКО после успешного коммита
        self.ensure_branch_at(slug, branch, commit.sha)
        return commit.sha

    def open_draft_pr(self, slug: str, branch: str, title: str, body: str,
                      base: str = "main", draft: bool = False) -> dict:
        """Создаёт PR. По умолчанию готовый к ревью (не draft) — решение шага 5:
        «одобрение» выражается мержем лида, поэтому draft только для эскалации."""
        repo = self.repo(slug)
        pr = repo.create_pull(title=title, body=body, head=branch, base=base,
                              draft=draft)
        return {"number": pr.number, "html_url": pr.html_url}

    # ------------------------------------------------- шаг 5: цикл ревью
    def find_pr(self, slug: str, branch: str) -> dict | None:
        """Открытый PR по ветке бота (для watcher'а). None — PR нет/закрыт."""
        repo = self.repo(slug)
        owner = slug.split("/")[0]
        for state in ("open", "closed"):
            prs = list(repo.get_pulls(state=state, head=f"{owner}:{branch}"))
            if prs:
                pr = prs[0]
                return {"number": pr.number, "url": pr.html_url, "state": pr.state,
                        "merged": bool(pr.merged), "draft": bool(pr.draft),
                        "title": pr.title}
        return None

    def bot_login(self) -> str:
        try:
            return self.whoami()
        except Exception:  # noqa: BLE001 — без логина просто не фильтруем автора
            return ""

    def list_comments(self, slug: str, number: int) -> list[dict]:
        """Issue + review-комментарии PR одним списком (шаг 5).

        Собираем оба канала: лид может ответить и обычным комментарием, и в
        review-треде конкретной строки diff-а.
        """
        pr = self.pr(slug, number)
        out: list[dict] = []
        for c in pr.get_issue_comments():
            out.append({"id": c.id, "kind": "issue", "author": c.user.login,
                        "body": c.body or "", "created_at": str(c.created_at),
                        "url": c.html_url})
        for c in pr.get_review_comments():
            out.append({"id": c.id, "kind": "review", "author": c.user.login,
                        "body": c.body or "", "created_at": str(c.created_at),
                        "url": c.html_url})
        out.sort(key=lambda x: x["created_at"])
        return out

    def comment(self, slug: str, number: int, body: str) -> dict:
        pr = self.pr(slug, number)
        c = pr.create_issue_comment(body)
        return {"id": c.id, "url": c.html_url}

    def review_ready(self, slug: str, number: int, body: str = "") -> None:
        """APPROVE-review: помечает PR как одобренный ботом (не мерж)."""
        pr = self.pr(slug, number)
        pr.create_review(body=body or "DocAgent: документация готова к мержу.",
                         event="APPROVE")

    def whoami(self) -> str:
        return self.api.get_user().login
