"""Пост-валидация черновика: антигаллюцинация и гигиена markdown.

Проверки дешёвые и детерминированные — их можно точечно переиспользовать
в LangGraph (шаг 3) как отдельный узел "guard".
"""

from __future__ import annotations

import re

from docagent.agent.schemas import DocDraft

# CamelCase / snake_case идентификаторы в backticks или как заголовок ### name(
_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{1,60})`|###\s+`?([A-Za-z_][A-Za-z0-9_.]*)\(")


def extract_cited_identifiers(draft: DocDraft) -> set[str]:
    out: set[str] = set()
    for m in _IDENT_RE.finditer(draft.content_md or ""):
        ident = m.group(1) or m.group(2)
        if not ident:
            continue
        head = ident.split(".")[-1]
        out.add(head)
    for s in draft.cited_symbols:
        out.add(s.split(".")[-1])
    return out


# служебные слова и markdown-токены, которые 3b-модели любят оборачивать в backticks
_STOPWORDS = {
    "md", "py", "json", "yaml", "yml", "toml", "ini", "cfg", "txt",
    "true", "false", "none", "null", "todo", "fixme",
    "bash", "shell", "console", "python", "diff", "http", "https", "pip",
    "args", "kwargs", "self", "cls", "etc", "e.g", "i.e", "vs", "ok", "id",
    "api", "adr", "pr", "ci", "cd", "url", "utf-8", "docker", "mcp", "llm",
    "ru", "en",
}


def check_hallucinated_symbols(draft: DocDraft, allowed: set[str]) -> list[str]:
    """Возвращает список идентификаторов, которых нет в allow-list (пусто = ок).

    Мягкий режим (для маленьких моделей): одиночные цитаты не отбрасывают черновик,
    а вырезаются из content_md (см. sanitize_draft_content). Строго отбраковываем
    только если после санитайза в тексте не осталось НИ ОДНОЙ разрешённой цитаты —
    это настоящий признак выдуманного API.
    """
    allowed_norm = {a.split(".")[-1] for a in allowed}
    cited = extract_cited_identifiers(draft)
    bad = sorted(c for c in cited if c.lower().strip(".,:;") not in _STOPWORDS
                 and c not in allowed_norm
                 and not c.startswith("NNNN"))
    return bad


def sanitize_draft_content(draft: DocDraft, allowed: set[str]) -> int:
    """Вырезает из content_md backtick-цитаты вне allow-list. Возвращает число удалений."""
    allowed_norm = {a.split(".")[-1] for a in allowed}

    def _repl(m: re.Match) -> str:
        ident = m.group(1)
        head = ident.split(".")[-1]
        if (head in allowed_norm or ident.lower().strip(".,:;") in _STOPWORDS
                or ident.startswith("NNNN")):
            return m.group(0)
        return ""  # безобидно удаляем вымышленное имя в backticks

    new_content, n_sub = _IDENT_BACKTICK_RE.subn(_repl, draft.content_md or "")
    if n_sub:
        draft.content_md = re.sub(r"[ \t]{2,}", " ", new_content)
    return n_sub


_IDENT_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{1,60})`")


def check_anchor_format(draft: DocDraft) -> str | None:
    """Якорь должен выглядеть как markdown-заголовок или быть null.

    Для create-экшена anchor не обязателен (файл/секция создаётся с нуля).
    """
    if draft.anchor is None:
        return None if draft.action.value in {"create", "remove"} else "anchor required for non-create action"
    if re.match(r"^#{1,6}\s+\S", draft.anchor):
        return None
    return f"anchor is not a markdown heading: {draft.anchor!r}"


def validate_draft_full(draft: DocDraft, allowed_symbols: set[str]) -> list[str]:
    """Список ошибок; пустой — черновик принимается.

    Антигаллюцинация в мягком режиме (qwen2.5-coder:3b): одиночные цитаты вне
    allow-list не убивают весь черновик, а вырезаются из текста (sanitize).
    Отбраковываем только полностью выдуманный текст: ни одной разрешённой цитаты
    и при этом есть вымышленные.
    """
    errors: list[str] = []
    bad = check_hallucinated_symbols(draft, allowed_symbols)
    n_removed = sanitize_draft_content(draft, allowed_symbols) if bad else 0
    remaining = extract_cited_identifiers(draft) & {a.split(".")[-1] for a in allowed_symbols}
    if bad and not remaining:
        errors.append(f"no allowed symbol cited anywhere; unknown identifiers: {bad[:8]}")
    anchor_err = check_anchor_format(draft)
    if anchor_err:
        errors.append(anchor_err)
    if len((draft.content_md or "").split()) > 400:
        errors.append("content_md too long (>400 words)")
    return errors
