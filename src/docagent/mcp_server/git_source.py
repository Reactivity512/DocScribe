"""Слой доступа к git: working tree / локальный репозиторий / GitHub API (опционально).

Возвращает пары ревизий (before, after) и содержимое файлов так, чтобы
анализаторы работали с чистыми данными независимо от источника.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class Revision:
    """Один снимок кода: либо working tree, либо коммит в репо, либо set файлов."""

    label: str
    files: dict[str, str] = field(default_factory=dict)  # path -> content (для 'files' режима)
    source: str = "working"  # working | repo | files | github

    def paths(self) -> list[str]:
        return sorted(self.files)


def _run(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=120, check=False,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as e:  # pragma: no cover
        raise GitError(f"git не найден: {e}") from e
    except subprocess.TimeoutExpired as e:  # pragma: no cover
        raise GitError(f"git timeout: {' '.join(args)}") from e
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args[1:])} -> rc={r.returncode}: {r.stderr.strip()[:400]}")
    return r.stdout


def is_git_repo(root: Path) -> bool:
    try:
        return _run(["git", "rev-parse", "--is-inside-work-tree"], root).strip() == "true"
    except GitError:
        return False


def resolve_repo_root(path: str | None = None) -> Path:
    """Абсолютный путь к корню репозитория/папки проекта."""
    p = Path(path or os.getcwd()).expanduser().resolve()
    if not p.exists():
        raise GitError(f"путь не существует: {p}")
    if is_git_repo(p):
        return Path(_run(["git", "rev-parse", "--show-toplevel"], p).strip())
    return p


def working_tree_diff(root: Path, staged: bool = False) -> str:
    args = ["git", "diff"]
    if staged:
        args.append("--cached")
    args += ["--no-color", "--unified=3"]
    return _run(args, root)


def diff_range(root: Path, base_ref: str, head_ref: str = "HEAD") -> str:
    return _run(
        ["git", "diff", "--no-color", "--unified=3", f"{base_ref}...{head_ref}"], root
    )


def diff_pr_branches(root: Path, pr_number: int, remote: str = "origin") -> tuple[str, str, str]:
    """Fetch PR из GitHub-репо и вернуть (diff, base_sha, head_sha)."""
    ref = f"refs/pull/{pr_number}/head"
    _run(["git", "fetch", "--quiet", remote, ref], root)
    head_sha = _run(["git", "rev-parse", "FETCH_HEAD"], root).strip()
    try:
        base_target = f"{remote}/HEAD"
        _run(["git", "rev-parse", "--verify", base_target], root)
    except GitError:
        base_target = "HEAD"
    base_sha = _run(["git", "merge-base", "FETCH_HEAD", base_target], root).strip()
    diff = _run(["git", "diff", "--no-color", "--unified=3", f"{base_sha}...{head_sha}"], root)
    return diff, base_sha, head_sha


def revision_from_repo(root: Path, ref: str | None = None) -> Revision:
    """Снимок всех .py/.md/.toml файлов репозитория на указанный ref (или HEAD)."""
    target = ref or _run(["git", "rev-parse", "HEAD"], root).strip()
    listing = _run(["git", "ls-tree", "-r", "--name-only", target], root)
    exts = (".py", ".pyi", ".md", ".rst", ".toml", ".cfg", ".txt", ".yaml", ".yml")
    files: dict[str, str] = {}
    for path in listing.splitlines():
        if not path.endswith(exts):
            continue
        try:
            files[path] = _run(["git", "show", f"{target}:{path}"], root)
        except GitError:
            files[path] = ""
    return Revision(label=target, files=files, source="repo")


def revision_from_dir(root: Path) -> Revision:
    """Снимок файловой папки без git (fallback для MVP/тестов)."""
    exts = (".py", ".pyi", ".md", ".rst", ".toml", ".cfg", ".txt", ".yaml", ".yml")
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in exts:
            continue
        rel = p.relative_to(root).as_posix()
        if any(x in rel.split("/") for x in {".git", "__pycache__", ".venv", "node_modules"}):
            continue
        try:
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            files[rel] = ""
    return Revision(label=f"dir:{root}", files=files, source="working")


# --------------------------------------------------------------------------- #
# GitHub REST (нужен только get_pull_request_context; работает офлайн-fallback)
# --------------------------------------------------------------------------- #
def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def gh_api(endpoint: str, accept: str = "application/vnd.github+json") -> dict | list:
    """Минимальный REST-клиент через curl (без доп. зависимостей в MVP)."""
    url = endpoint if endpoint.startswith("http") else f"https://api.github.com/{endpoint.lstrip('/')}"
    tok = github_token()
    cmd = ["curl", "-sSL", "--max-time", "30", "-H", f"Accept: {accept}"]
    if tok:
        cmd += ["-H", f"Authorization: Bearer {tok}"]
    cmd += [url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise GitError(f"GitHub API недоступен: {r.stderr.strip()[:200]}")
    import json as _json

    payload = _json.loads(r.stdout or "{}")
    if isinstance(payload, dict) and payload.get("message"):
        raise GitError(f"GitHub API: {payload['message']}")
    return payload


def github_compare(repo: str, base: str, head: str) -> dict:
    return gh_api(f"repos/{repo}/compare/{base}...{head}")


def build_diff_from_compare(comp: dict) -> str:
    """Собрать unified diff из compare API (когда git fetch невозможен)."""
    chunks: list[str] = []
    for f in comp.get("files", []):
        patch = f.get("patch")
        if not patch:
            chunks.append(f"diff --git a/{f['filename']} b/{f['filename']}\n(Binary or too large)\n")
            continue
        old = f.get("previous_filename", f["filename"])
        header = f"diff --git a/{old} b/{f['filename']}\n--- a/{old}\n+++ b/{f['filename']}\n"
        body = "\n".join(
            ln if ln[:1] in "+-" else " " + ln for ln in patch.splitlines()
        )
        chunks.append(header + body + "\n")
    return "\n".join(chunks)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SAFE_REF = re.compile(r"^[A-Za-z0-9_./@^~:+-]{1,200}$")


def check_ref(ref: str) -> str:
    """Защита от инъекций в аргументах git."""
    if not _SAFE_REF.match(ref or ""):
        raise GitError(f"недопустимый ref: {ref!r}")
    return ref
