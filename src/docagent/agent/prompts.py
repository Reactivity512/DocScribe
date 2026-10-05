"""Промпт-политика для маленьких моделей (qwen2.5-coder:3b).

Принципы (§3 обсуждения шага 2):
- короткий системный промпт + жёсткий JSON-контракт;
- один вызов = один target;
- few-shot пример прямо в системном промпте (3b лучше держит формат с примером);
- антигаллюцинация: имена API разрешено брать ТОЛЬКО из блока ALLOWED_SYMBOLS;
- low temperature (0.1) — на уровне Settings.
"""

from __future__ import annotations

import json

from docagent.mcp_server.models import DocsDecision, ExpectedDocsTarget

TARGET_FILE_HINT: dict[ExpectedDocsTarget, str] = {
    ExpectedDocsTarget.readme: "README.md",
    ExpectedDocsTarget.api_reference: "docs/api-reference.md",
    ExpectedDocsTarget.adr: "docs/adr/NNNN-slug.md",
    ExpectedDocsTarget.guide: "docs/guide/<topic>.md",
    ExpectedDocsTarget.changelog: "CHANGELOG.md",
}

_LANG_RULE = {
    "ru": "Пиши документацию на русском языке.",
    "en": "Write the documentation in English.",
}

_SYSTEM_TEMPLATE = """You are a technical writer for a Python project. You update documentation to match code changes.

HARD RULES:
1. Reply with ONE JSON object only. No prose before or after. No markdown fences needed.
2. JSON schema (all keys required unless noted):
{{"target": "{target}", "action": "create|append|update|remove", "file_path": "...", \
"anchor": "exact section heading or null", "content_md": "markdown text", \
"rationale": "one sentence why", "confidence": 0.0-1.0, "cited_symbols": ["..."]}}
3. NEVER invent API names. Cite ONLY identifiers from the ALLOWED_SYMBOLS list. \
Never write dotted module paths (like pkg.module.func) — use the bare name from ALLOWED_SYMBOLS. \
If you cite an identifier not in that list, your answer is rejected.
4. content_md must be self-contained, factual, <= {max_words} words, matching the diff evidence only.
5. {lang_rule}
6. anchor rules: for action "append"/"update" set anchor to an existing heading of the target file \
(copy it exactly from CURRENT DOC SECTION); if the file has no sections yet or action is "create", \
set anchor to null. Never put markdown fences inside anchor.
7. If nothing meaningful can be written from the evidence, return:
{{"skipped_reason": "..."}}

EXAMPLE reply:
{{"target": "api-reference", "action": "append", "file_path": "docs/api-reference.md", \
"anchor": "## Exporters", "content_md": "### `to_parquet(df, path)`\\n\\nWrites a DataFrame to \
Parquet. `path` must end with `.parquet`.", "rationale": "new public function added in this PR", \
"confidence": 0.8, "cited_symbols": ["to_parquet"]}}"""


def build_system(target: str, lang: str, max_words: int = 180) -> str:
    return _SYSTEM_TEMPLATE.format(
        target=target,
        max_words=max_words,
        lang_rule=_LANG_RULE.get(lang, _LANG_RULE["en"]),
    )


def build_user_prompt(
    *,
    target: ExpectedDocsTarget,
    decision: DocsDecision,
    diff_excerpt: str,
    allowed_symbols: list[str],
    existing_section: str | None,
    lang: str,
) -> str:
    """Пользовательская часть: контекст с помеченными строками (стабильно парсится и fake-бэкендом)."""
    ev_lines = [f"- {e.file}" + (f" ({e.symbol.split('.')[-1]})" if e.symbol else "") + f": {e.detail}"
                for e in decision.evidence[:8]]
    parts = [
        f"TARGET: {target.value}",
        f"FILE_PATH: {TARGET_FILE_HINT.get(target, 'docs/notes.md')}",
        f"LANG: {lang}",
        f"TRIGGERS: {', '.join(t.value for t in decision.trigger_kinds)}",
        f"SUMMARY: {'; '.join(ev_lines) or decision.notes}",
        "",
        "ALLOWED_SYMBOLS (the ONLY identifiers you may name):",
        json.dumps(allowed_symbols[:60], ensure_ascii=False),
        "",
        "DIFF EXCERPT:",
        "```diff",
        diff_excerpt.strip()[:6000] or "(empty)",
        "```",
    ]
    if existing_section:
        parts += ["", "CURRENT DOC SECTION (update it, do not duplicate):", "```markdown",
                  existing_section.strip()[:3000], "```"]
    parts += ["", "Return the JSON object now."]
    return "\n".join(parts)


RETRY_SUFFIX = (
    "\n\nYour previous reply was rejected: {error}. "
    "Fix exactly that and reply again with a valid JSON object only."
)
