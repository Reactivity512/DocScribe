"""Детерминированный bootstrap-сканер (без LLM).

Составляет план поэтапной генерации документации для проекта, где docs/ нет или пуста.
Порядок стейджей и чанков считается здесь, LLM только пишет текст внутри чанка —
это снижает галлюцинации 3b-модели (решение лида, шаг 0 §5).
"""

from __future__ import annotations

import re
from pathlib import Path

from docagent.mcp_server.analyzers.diff_parser import is_code_path, is_doc_path
from docagent.mcp_server.analyzers.python_ast import symbols_from_source
from docagent.mcp_server.models import (
    BootstrapPlan,
    BootstrapStageDef,
    DocStatus,
)

_CORE_FILES = ["README.md"]


def scan_docs_status(files: dict[str, str], docs_dir: str = "docs") -> DocStatus:
    doc_files = sorted(p for p in files if is_doc_path(p))
    has_readme = any(p.lower() == "readme.md" or p.endswith("/readme.md") for p in files)
    has_api = any("api-reference" in p.lower() or "api_reference" in p.lower() for p in doc_files)
    has_adr = any("/adr/" in f"/{p}" or p.lower().startswith("adr/") or re.search(r"adr[-_]", p, re.I) for p in doc_files)
    has_changelog = any("changelog" in p.lower() or "history" in p.lower() for p in doc_files)

    code_paths = [p for p in files if is_code_path(p)]
    with_docstring = 0
    for p in code_paths:
        syms, _, _ = symbols_from_source(files[p], p)
        pub = [s for s in syms if s.public and s.kind in ("function", "class")]
        if pub and all(s.has_docstring for s in pub):
            with_docstring += 1
    coverage = round(with_docstring / len(code_paths), 3) if code_paths else 0.0

    return DocStatus(
        docs_exists=bool(doc_files),
        docs_dir=docs_dir,
        has_readme=has_readme,
        has_api_reference=has_api,
        has_adr=has_adr,
        has_changelog=has_changelog,
        existing_files=doc_files,
        coverage_ratio=coverage,
    )


def modules_of(files: dict[str, str]) -> list[dict]:
    """Пакеты/модули с числом публичных символов (сортировка по объёму API)."""
    out = []
    for path in sorted(files):
        if not is_code_path(path) or path.endswith(".pyi"):
            continue
        src = files[path]
        syms, mod_docs, _ = symbols_from_source(src, path)
        pub = [s for s in syms if s.public]
        if not pub and not mod_docs:
            continue
        out.append({
            "file": path,
            "module": path.rsplit(".py", 1)[0].replace("/", "."),
            "public_symbols": [s.name for s in pub],
            "public_count": len(pub),
            "lines": len(src.splitlines()),
            "has_module_docstring": bool(mod_docs),
        })
    out.sort(key=lambda m: (-m["public_count"], m["file"]))
    return out


def chunk_modules(modules: list[dict], max_chunk_lines: int = 120) -> list[list[dict]]:
    """Грейдим модули в чанки ≤max_chunk_lines строк (1 секция API-ref = 1 чанк)."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for m in modules:
        if cur and size + m["lines"] > max_chunk_lines:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += m["lines"]
    if cur:
        chunks.append(cur)
    return chunks


def build_plan(
    files: dict[str, str],
    *,
    docs_dir: str = "docs",
    max_chunk_lines: int = 120,
    adr_candidates_threshold: int = 1,
) -> BootstrapPlan:
    """Главная функция сканера: дерево файлов → план из 5 стейджей (§5 плана шага 0)."""
    status = scan_docs_status(files, docs_dir)
    modules = modules_of(files)
    total_public = sum(m["public_count"] for m in modules)

    # кандидаты на ADR: конфиги сборки, слои архитектуры, внешние интеграции
    adr_hits = []
    for p in sorted(files):
        base = p.split("/")[-1]
        if base in {"pyproject.toml", "setup.py", "Dockerfile", "docker-compose.yml"}:
            adr_hits.append((p, "процесс сборки/развёртывания"))
        if re.search(r"(^|/)(db|storage|security|auth|api|clients?)/", p) and is_code_path(p):
            adr_hits.append((p, "выделенная подсистема"))
        if base.startswith("test_"):
            continue

    chunks = chunk_modules(modules, max_chunk_lines)

    stages: list[BootstrapStageDef] = [
        BootstrapStageDef(
            stage=1,
            name="structure_index",
            title="Каркас docs/ + index",
            target_files=[f"{docs_dir}/index.md", f"{docs_dir}/adr/template.md"],
            rationale="нулевой контент, только структура — безопасно для 3b-модели",
        ),
        BootstrapStageDef(
            stage=2,
            name="readme",
            title="README: установка и quickstart из кода/pyproject",
            target_files=["README.md"],
            chunks=[{"file": p} for p in sorted(files) if p.split("/")[-1] in {
                "pyproject.toml", "setup.py", "requirements.txt", "__init__.py", ".env.example",
            }],
            rationale="детерминированные данные: зависимости, entry points, версии",
        ),
        BootstrapStageDef(
            stage=3,
            name="api_reference",
            title=f"API reference по модулям ({len(chunks)} чанк(ов))",
            target_files=[f"{docs_dir}/api-reference.md"],
            chunks=[
                {"chunk": i + 1, "modules": [m["file"] for m in c],
                 "symbols": [s for m in c for s in m["public_symbols"]]}
                for i, c in enumerate(chunks)
            ],
            rationale="чанкование ≤{} строк ограничивает контекст модели".format(max_chunk_lines),
        ),
        BootstrapStageDef(
            stage=4,
            name="adr_skeletons",
            title=f"ADR-скелеты по кандидатам из структуры кода ({len(adr_hits)})",
            target_files=[f"{docs_dir}/adr/NNNN-<slug>.md"],
            chunks=[{"file": p, "reason": r} for p, r in adr_hits[:8]],
            rationale="только скелет (контекст/решение/последствия), вывод пишет человек",
        ),
        BootstrapStageDef(
            stage=5,
            name="changelog",
            title="Changelog по git log (группы помечаются Proposed)",
            target_files=[f"{docs_dir}/CHANGELOG.md"],
            rationale="нужен git-источник; без него stage пропускается",
        ),
    ]

    mode = "bootstrap" if not status.docs_exists or total_public == 0 else "incremental"
    if status.has_api_reference and status.has_readme and status.has_adr and status.has_changelog:
        mode = "incremental"
        stages = []

    return BootstrapPlan(
        mode=mode,
        status=status,
        stages=stages,
        modules_scanned=len(modules),
        public_symbols_total=total_public,
        notes=(
            f"docs/: {'есть' if status.docs_exists else 'нет'}; покрытие docstring'ами "
            f"{status.coverage_ratio:.0%}; модулей с публичным API: {len(modules)}"
        ),
    )
