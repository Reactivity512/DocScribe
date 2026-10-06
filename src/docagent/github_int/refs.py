"""Разбор ссылок на PR и резолв клиента (шаги 4-5).

Одна точка правды для «owner/repo», номера PR и клиента GitHub — используется
нодой fetch_pr (шаг 4), publisher'ом (publish) и watcher'ом (шаг 5), чтобы
правила разбора не разъезжались между модулями.
"""
from __future__ import annotations

import re

from ..config import Settings

# номер PR в URL: ".../pull/7" или ".../pulls/7" (в т.ч. с /files, query, fragment)
_PR_URL_RE = re.compile(r"pulls?/(?P<num>\d+)")
# "owner/name#7"
_HASH_RE = re.compile(r"^(?P<slug>[\w.-]+/[\w.-]+)#(?P<num>\d+)$")
# не-slug: "pull", "pulls", "issues", "tree" в конце ("owner/repo/pulls")
_NON_SLUG_TAIL = frozenset({"pull", "pulls", "issues", "issue", "tree", "blob"})


def _looks_like_path(pr_ref: str) -> bool:
    """Локальный путь (F:\\..., /tmp/x.diff, data/work/a.diff) — не slug и не PR.

    Диск различаем по односимвольному префиксу (F:\\, C:/) — схема URL (https:)
    двоеточием на путь не считается.
    """
    if pr_ref.startswith(("/", "\\")):
        return True
    head = pr_ref.split("/", 1)[0]
    if len(head) == 2 and head[1] == ":" and head[0].isalpha():
        return True
    return pr_ref.endswith(".diff")


def _clean_ref(ref: str) -> str:
    """URL -> путь: срезаем схему, хост и хвостовой слэш.

    Хост срезаем только для github.com — «gitlab.com/o/r/pull/7» иначе выглядел бы
    как слаг «gitlab.com/o» с номером 7, и publish ушёл бы в чужой «репозиторий».
    """
    for prefix in ("https://", "http://"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            if not ref.startswith("github.com/"):
                return ""
            ref = ref[len("github.com/"):]
            break
    return ref.rstrip("/")


def _prune_repo_tail(ref: str) -> str | None:
    """'o/r/tree/main' -> 'o/r', 'o/r.git' -> 'o/r', 'o/r/pulls' -> None."""
    ref = ref[:-len(".git")] if ref.endswith(".git") else ref
    parts = [p for p in ref.split("/") if p]
    if len(parts) < 2 or parts[-1].lower() in _NON_SLUG_TAIL:
        return None
    return f"{parts[0]}/{parts[1]}"


def parse_pr_ref(pr_ref: str) -> tuple[str | None, int | None]:
    """(slug, номер PR) из ссылки любого поддерживаемого вида.

    Понимает: URL /pull/N (в т.ч. /files, query, fragment), owner/repo#N,
    owner/repo (номер None), owner/repo/tree/main, git@github.com:owner/repo.git.
    Возвращает (None, None) для локальных путей, gold-кейсов и чужих URL.
    """
    ref = (pr_ref or "").strip()
    if not ref or _looks_like_path(ref):
        return None, None
    if ref.startswith("git@"):
        ref = ref.split(":", 1)[-1]  # git@github.com:owner/name.git
    ref = _clean_ref(ref)
    m = _HASH_RE.match(ref)
    if m:
        return m.group("slug"), int(m.group("num"))
    url_pr = _PR_URL_RE.search(ref)
    slug = _prune_repo_tail(ref)
    if slug is None:
        return None, None
    return slug, int(url_pr.group("num")) if url_pr else None


def repo_and_pr(reference: str) -> tuple[str | None, int | None]:
    """(slug, номер) из любого вида ссылки, включая «owner/repo#N» без схемы.

    `parse_pr_ref` разбирает URL/gold/пути; форма «slug#N» — сокращение, которым
    CLI и watcher обмениваются внутри проекта. Держим оба вида в одной функции,
    иначе watcher молча уходил бы на поиск PR по ветке вместо прямого номера.
    """
    ref = (reference or "").strip()
    if "#" in ref:
        head, _, tail = ref.rpartition("#")
        if tail.isdigit():
            slug, _num = parse_pr_ref(head)
            if slug:
                return slug, int(tail)
    return parse_pr_ref(ref)


def _repo_slug(payload: dict, s: Settings) -> str:
    """Слаг целевого репозитория: PR-ссылка, иначе DEMO_REPO (fallback publisher'а)."""
    slug, _num = parse_pr_ref(payload.get("pr_ref", ""))
    return slug or s.demo_repo


def pr_slug(state: dict, s: Settings) -> str:
    """Слаг для состояния графа: сначала уже разрешённый fetch_pr, потом из ссылки."""
    if state.get("pr_slug"):
        return state["pr_slug"]
    return parse_pr_ref(state.get("pr_ref", ""))[0] or s.demo_repo


def pr_number(state: dict) -> int | None:
    if state.get("pr_number"):
        return int(state["pr_number"])
    return parse_pr_ref(state.get("pr_ref", ""))[1]


def title_for_pr(state: dict) -> str:
    if state.get("pr_title"):
        return state["pr_title"]
    num = pr_number(state)
    slug = state.get("pr_slug") or ""
    if num:
        return f"Docs: {slug}#{num} (автообновление документации)"
    return f"Docs: {state.get('pr_ref', '') or state.get('thread_id', 'auto')}"


def make_client(state: dict | None = None, s: Settings | None = None, client=None):
    """Клиент для GitHub-операций: инжектированный (тесты) либо по токену.

    Импорт клиента ленивый: класс импортируется на уровне модуля в publisher.py
    ломал подмену GitHubClient в тестах (модуль держал старую ссылку).
    """
    if client is not None:
        return client
    from .client import GitHubClient
    return GitHubClient(settings=s)
