"""Распознавание bootstrap-входов (не-unified-diff) и статусы docs/ из них.

Gold-сет шага 0 (§5 плана) фиксирует: первый запуск системы — это НЕ diff, а
«снимок сканера» репозитория (`# bootstrap scan (not a diff)`). Такие кейсы имеют
ожидаемое поведение `escalate_bootstrap`: агент не пишет правку в PR, а запускает
поэтапный bootstrap документации (5 стейджей).

Два входа в модуль:
  * `looks_like_bootstrap_scan(text)` — детектор формата (маркер + отсутствие
    unified-diff заголовков). Возвращает False для настоящего diff, чтобы не
    угнать обычный PR в bootstrap;
  * `parse_scan(text)` — извлечение фактов о docs/ из снимка: есть ли docs_dir,
      README, api-reference, ADR, changelog, index. Нужен, чтобы понять, на каком
      стейдже bootstrap мы находимся (docs/index.md EXISTS (stage 1 done) → не
      первый запуск, но проект всё ещё недодокументирован).
"""

from __future__ import annotations

import re

MARKER_RE = re.compile(r"bootstrap\s+scan", re.I)
_DIFF_HEADER_RE = re.compile(r"^(---\s+[ab]/|\+\+\+\s+[ab]/|diff\s+--git\s)", re.M)


_NO_DIFF_MARKER_RE = re.compile(r"no\s+diff|initial\s+repository\s+scan", re.I)


def looks_like_bootstrap_scan(text: str) -> bool:
    if not text:
        return False
    has_marker = bool(MARKER_RE.search(text))
    has_no_diff = bool(_NO_DIFF_MARKER_RE.search(text))
    if not (has_marker or has_no_diff):
        return False
    # bootstrap-вход — это НЕ unified diff; если есть заголовки файлов, решаем как diff
    return not _DIFF_HEADER_RE.search(text)


_KV = {
    "docs_missing": re.compile(r"^docs_dir:\s*MISSING", re.I | re.M),
    "docs_exists": re.compile(r"docs/\S*\s+EXISTS|^docs_dir:\s*(?!MISSING)\S+", re.I | re.M),
    "index": re.compile(r"docs/index\.md", re.I),
    "api_reference": re.compile(r"docs/api[-_]reference\.md", re.I),
    "adr": re.compile(r"docs/adr/", re.I),
    "changelog": re.compile(r"docs/changelog\.md", re.I),
    "readme_lines": re.compile(r"^\s*readme:\s*(\d+)\s*line", re.I | re.M),
    "readme_current": re.compile(r"readme_current:", re.I),
    "stage_done": re.compile(r"stage\s+(\d+)\s+(?:done|complete)", re.I),
    "scaffold_only": re.compile(r"scaffold\s+only", re.I),
    "public_symbols": re.compile(r"^\s*([\w.,\-\s]+?)(?:\n\S|$)", re.M),
}


def parse_scan(text: str) -> dict:
    """Факты из bootstrap-снимка: чего не хватает и на каком стейдже мы."""
    # Stage 0: пустой репозиторий — «no diff / initial repository scan», без docs/
    if _NO_DIFF_MARKER_RE.search(text) and not MARKER_RE.search(text):
        return {
            "is_first_run": True,
            "has_docs_dir": False,
            "has_index": False,
            "has_api_reference": False,
            "has_adr": False,
            "has_changelog": False,
            "readme_lines": None,
            "stages_done": [],
            "api_scaffold_only": False,
            "packages_scanned": 0,
            "deps_declared": False,
            "missing_sections": sorted({
                "README (install/quickstart)",
                "docs/index.md",
                "docs/api-reference.md",
                "docs/adr/",
                "docs/changelog.md",
            }),
        }

    has_docs_missing = bool(_KV["docs_missing"].search(text))
    has_index = bool(_KV["index"].search(text)) and not has_docs_missing
    readme_m = _KV["readme_lines"].search(text)
    readme_lines = int(readme_m.group(1)) if readme_m else None
    stage_done = [int(m.group(1)) for m in _KV["stage_done"].finditer(text)]
    scaffold = bool(_KV["scaffold_only"].search(text))
    n_pkg = len(re.findall(r"\(\d+ modules?,\s*\d+ public symbols?\)", text))
    deps = re.search(r"dependencies:\s*(.+)", text)

    missing = []
    if has_docs_missing or not has_index:
        missing.append("docs/index.md")
    if has_docs_missing or not _KV["api_reference"].search(text) or scaffold:
        missing.append("docs/api-reference.md")
    if has_docs_missing or not _KV["adr"].search(text):
        missing.append("docs/adr/")
    if has_docs_missing or not _KV["changelog"].search(text):
        missing.append("docs/changelog.md")
    if readme_lines is not None and readme_lines < 12:
        missing.append("README (install/quickstart)")
    elif _KV["readme_current"].search(text) and has_docs_missing:
        missing.append("README (install/quickstart)")

    return {
        "is_first_run": bool(has_docs_missing),
        "has_docs_dir": not has_docs_missing,
        "has_index": has_index,
        "has_api_reference": bool(_KV["api_reference"].search(text)) and not scaffold,
        "has_adr": bool(_KV["adr"].search(text)),
        "has_changelog": bool(_KV["changelog"].search(text)),
        "readme_lines": readme_lines,
        "stages_done": sorted(set(stage_done)),
        "api_scaffold_only": scaffold,
        "packages_scanned": n_pkg,
        "deps_declared": bool(deps),
        "missing_sections": sorted(set(missing)),
    }
