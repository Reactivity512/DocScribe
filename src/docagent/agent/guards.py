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


def check_hallucinated_symbols(draft: DocDraft, allowed: set[str]) -> list[str]:
    """Возвращает список идентификаторов, которых нет в allow-list (пусто = ок)."""
    allowed_norm = {a.split(".")[-1] for a in allowed}
    cited = extract_cited_identifiers(draft)
    stop = {"md", "py", "json", "yaml", "yml", "true", "false", "none"}
    bad = sorted(c for c in cited if c.lower() not in stop and c not in allowed_norm
                 and not c.startswith("NNNN"))
    return bad


def check_anchor_format(draft: DocDraft) -> str | None:
    """Якорь должен выглядеть как markdown-заголовок или быть null."""
    if draft.anchor is None:
        return None if draft.action.value == "create" else "anchor required for non-create action"
    if re.match(r"^#{1,6}\s+\S", draft.anchor):
        return None
    return f"anchor is not a markdown heading: {draft.anchor!r}"


def validate_draft_full(draft: DocDraft, allowed_symbols: set[str]) -> list[str]:
    """Список ошибок; пустой — черновик принимается."""
    errors: list[str] = []
    bad = check_hallucinated_symbols(draft, allowed_symbols)
    if bad:
        errors.append(f"cited identifiers not in ALLOWED_SYMBOLS: {bad}")
    anchor_err = check_anchor_format(draft)
    if anchor_err:
        errors.append(anchor_err)
    if len((draft.content_md or "").split()) > 400:
        errors.append("content_md too long (>400 words)")
    return errors
