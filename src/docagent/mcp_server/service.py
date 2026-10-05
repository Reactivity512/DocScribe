"""Сервисный слой: общие операции над репозиторием/diff для MCP-инструментов.

Инструменты в server.py — тонкие обёртки над этим модулем (легко переиспользовать
напрямую из LangGraph на шаге 3 без MCP-транспорта).
"""

from __future__ import annotations

import re
from pathlib import Path

from docagent.config import Settings, get_settings
from docagent.mcp_server import git_source as gs
from docagent.mcp_server.analyzers import (
    api_diff,
    bootstrap_scanner,
    bootstrap_text,
    diff_parser,
    triggers,
)
from docagent.mcp_server.models import (
    ApiChange,
    ApiDiffResult,
    BootstrapPlan,
    ChangedSymbol,
    DecisionBehavior,
    DiffAnalysis,
    DiffStats,
    DocsDecision,
    Evidence,
    ExpectedDocsTarget,
    FileContent,
    FileStat,
    SymbolInfo,
)


class ServiceError(RuntimeError):
    pass


def _resolve_root(repo_path: str | None) -> Path:
    try:
        return gs.resolve_repo_root(repo_path)
    except gs.GitError as e:
        raise ServiceError(str(e)) from e


# --------------------------------------------------------------------------- #
# Получение diff по трём способам
# --------------------------------------------------------------------------- #
def obtain_diff(
    *,
    repo_path: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    diff_text: str | None = None,
    pr_number: int | None = None,
    staged: bool = False,
    settings: Settings | None = None,
) -> tuple[str, str, str]:
    """Возвращает (diff_text, before_label, after_label)."""
    s = settings or get_settings()
    if diff_text:
        return diff_text, "base(diff)", "head(diff)"

    root = _resolve_root(repo_path)
    try:
        if pr_number is not None and gs.is_git_repo(root):
            d, base_sha, head_sha = gs.diff_pr_branches(root, pr_number)
            return d, base_sha, head_sha
        if gs.is_git_repo(root):
            if base_ref:
                return (
                    gs.diff_range(root, gs.check_ref(base_ref), gs.check_ref(head_ref or "HEAD")),
                    base_ref,
                    head_ref or "HEAD",
                )
            return gs.working_tree_diff(root, staged=staged), "HEAD", "working-tree"
        # не-git: сравниваем папку с переданным эталоном невозможен → просим diff_text
        raise ServiceError(
            f"{root} не является git-репозиторием; передайте diff_text или работайте через git"
        )
    except gs.GitError as e:
        raise ServiceError(str(e)) from e


def revisions_for_pair(
    root: Path, before_label: str, after_label: str, *, allow_dir: bool = True
) -> tuple[gs.Revision, gs.Revision] | None:
    """Полные деревья файлов до/после, если оба ref разрешимы локально."""
    if not gs.is_git_repo(root):
        if allow_dir and before_label == after_label:
            rev = gs.revision_from_dir(root)
            return rev, rev
        return None
    try:
        before = gs.revision_from_repo(root, _try_resolve(root, before_label))
        after = gs.revision_from_repo(root, _try_resolve(root, after_label))
        return before, after
    except gs.GitError:
        return None


def _try_resolve(root: Path, label: str) -> str | None:
    if label in ("working-tree", "base(diff)", "head(diff)"):
        return None
    try:
        return gs._run(["git", "rev-parse", "--verify", label], root).strip()
    except gs.GitError:
        return None


# --------------------------------------------------------------------------- #
# Публичный API / анализ
# --------------------------------------------------------------------------- #
def api_snapshot(root: Path, ref: str | None = None) -> list[SymbolInfo]:
    rev = gs.revision_from_repo(root, ref) if gs.is_git_repo(root) else gs.revision_from_dir(root)
    syms, _errors = api_diff.snapshot_files(rev.files, root if gs.is_git_repo(root) else None)
    # ошибки парсинга не роняют снапшот: битые модули просто не дают символов
    return [s for s in syms if s.public]


def api_diff_full(root: Path, before_label: str, after_label: str) -> ApiDiffResult | None:
    pair = revisions_for_pair(root, before_label, after_label)
    if not pair:
        return None
    before, after = pair
    b_syms, _ = api_diff.snapshot_files(before.files, root)
    a_syms, _ = api_diff.snapshot_files(after.files, root)
    recs = api_diff.diff_snapshots(b_syms, a_syms)
    return _to_api_result(recs, before.label, after.label)


def _to_api_result(recs: list[api_diff.ApiChangeRecord], b: str, a: str) -> ApiDiffResult:
    changes = [
        ApiChange(
            kind=r.kind,
            symbol=r.symbol,
            file=r.file,
            old_signature=r.old_signature,
            new_signature=r.new_signature,
            param_diff=r.param_diff,
            breaking=r.breaking,
            reasons=r.reasons,
        )
        for r in recs
        if r.symbol_public and r.kind != "unchanged"
    ]
    return ApiDiffResult(
        before_ref=b,
        after_ref=a,
        changes=changes,
        breaking_count=sum(1 for c in changes if c.breaking),
    )


def build_stats(parse: diff_parser.DiffParse) -> DiffStats:
    buckets = triggers.classify_files(parse)
    files = []
    for f in parse.files:
        files.append(
            FileStat(
                path=f.path,
                additions=f.additions,
                deletions=f.deletions,
                is_doc=f.path in buckets["doc"],
                is_code=f.path in buckets["code"],
                is_test=f.path in buckets["test"],
                is_ci=f.path in buckets["ci"],
                is_config=f.path in buckets["config"],
                language=diff_parser.language_of(f.path),
            )
        )
    return DiffStats(
        files=files,
        total_additions=sum(f.additions for f in parse.files),
        total_deletions=sum(f.deletions for f in parse.files),
        code_files=len(buckets["code"]),
        doc_files=len(buckets["doc"]),
        test_files=len(buckets["test"]),
        ci_files=len(buckets["ci"]),
        config_files=len(buckets["config"]),
        truncated=parse.truncated,
    )


_DOC_REF_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)|docs/[A-Za-z0-9_./-]+\.(?:md|rst)")


def analyze_diff(diff_text: str, s: Settings) -> tuple[DiffAnalysis, diff_parser.DiffParse, list[api_diff.ApiChangeRecord]]:
    parse = diff_parser.parse_unified_diff(diff_text, max_bytes=s.max_diff_bytes)
    recs, errors = api_diff.diff_from_parsed_files(parse.files)
    changed_symbols = [
        ChangedSymbol(
            name=r.symbol,
            qualified_name=r.symbol,
            file=r.file,
            change=r.kind,
            public=r.symbol_public,
        )
        for r in recs
    ]
    refs: set[str] = set()
    for f in parse.files:
        for line in f.added_lines + f.removed_lines:
            for m in _DOC_REF_RE.finditer(line):
                refs.add(m.group(1) or m.group(0))
    buckets = triggers.classify_files(parse)
    summary = _make_summary(parse, buckets, recs)
    analysis = DiffAnalysis(
        summary=summary,
        stats=build_stats(parse),
        changed_symbols=changed_symbols,
        dependencies_added=diff_parser.extract_added_dependencies(parse),
        env_vars_touched=diff_parser.extract_env_vars(parse),
        doc_references=sorted(refs)[:50],
        hunk_headers=[h for f in parse.files for h in f.hunk_headers][:80],
        text_deltas={
            f.path: diff_parser.multiset_delta(f.added_lines, f.removed_lines)
            for f in parse.files
            if diff_parser.is_doc_path(f.path)
        },
        parse_errors=errors + parse.errors,
    )
    return analysis, parse, recs


def _make_summary(parse, buckets: dict[str, list[str]], recs: list[api_diff.ApiChangeRecord]) -> str:
    parts = [f"{len(parse.files)} файл(ов): код {len(buckets['code'])}, док {len(buckets['doc'])}, "
             f"тесты {len(buckets['test'])}, CI {len(buckets['ci'])}, конфиг {len(buckets['config'])}"]
    pub = [r for r in recs if r.symbol_public]
    if pub:
        parts.append("изменения публичного API: " + ", ".join(
            f"{r.symbol} ({r.kind.value})" for r in pub[:6]
        ))
    else:
        parts.append("публичный API не затронут")
    return "; ".join(parts)


def decide(
    parse: diff_parser.DiffParse,
    recs: list[api_diff.ApiChangeRecord],
    s: Settings,
) -> DocsDecision:
    beh, detail, file = api_diff.behavior_delta(parse.files)
    return triggers.summarize(
        parse,
        api_changes=recs,
        has_behavior_delta=beh,
        behavior_detail=detail,
        behavior_file=file,
        typo_max_changed_lines=s.typo_max_changed_lines,
        typo_min_new_words=s.typo_min_new_words,
        truncated=parse.truncated,
    )


# --------------------------------------------------------------------------- #
# Bootstrap-эскалация: вход «не diff» (снимок сканера из gold-сета §5 плана)
# --------------------------------------------------------------------------- #
def decide_on_diff(diff_text: str, s: Settings | None = None) -> DocsDecision:
    """Единая точка принятия решения по тексту входа (diff ИЛИ bootstrap-снимок).

    Порядок важен: сначала проверяем формат входа. Если это bootstrap-скан,
    возвращаем `escalate_bootstrap` — обычный анализ diff по такому тексту дал бы
    мусор (там нет unified-diff заголовков).
    """
    s = s or get_settings()
    if bootstrap_text.looks_like_bootstrap_scan(diff_text):
        return bootstrap_escalation_from_scan(diff_text, s)
    _analysis, parse, recs = analyze_diff(diff_text, s)
    return decide(parse, recs, s)


def bootstrap_escalation_from_scan(scan_text: str, s: Settings | None = None) -> DocsDecision:
    """DocsDecision для bootstrap-входа: какой стейдж следующий и чего не хватает."""
    s = s or get_settings()
    facts = bootstrap_text.parse_scan(scan_text)
    stage = 1
    for done in facts["stages_done"]:
        stage = max(stage, done + 1)
    if facts["api_scaffold_only"] and stage < 3:
        stage = 3
    missing = list(facts["missing_sections"])
    targets_map = {
        1: [ExpectedDocsTarget.api_reference],
        2: [ExpectedDocsTarget.readme],
        3: [ExpectedDocsTarget.api_reference],
        4: [ExpectedDocsTarget.adr],
        5: [ExpectedDocsTarget.changelog],
    }
    return DocsDecision(
        should_update=False,  # в PR ничего не пишем: это отдельный bootstrap-конвейер
        behavior=DecisionBehavior.escalate_bootstrap,
        bootstrap_stage=stage,
        bootstrap_missing=missing,
        ignore_reasons=[],
        confidence=0.95,
        severity="high" if facts["is_first_run"] else "medium",
        expected_docs_targets=targets_map.get(stage, []),
        evidence=[
            Evidence(
                file="<bootstrap-scan>",
                detail=(
                    f"[R-bootstrap] снимок репозитория вместо diff; docs_dir "
                    f"{'нет' if facts['is_first_run'] else 'есть'}, следующий стейдж {stage}; "
                    f"не хватает: {missing}"
                ),
            )
        ],
        rules_fired=["R-bootstrap-scan"],
        needs_llm_verification=True,
        notes="эскалация на поэтапный bootstrap документации (5 стейджей)",
    )


def read_file_at(root: Path, path: str, ref: str | None, s: Settings) -> FileContent:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise ServiceError(f"недопустимый путь: {path}")
    if gs.is_git_repo(root) and ref and ref not in ("working-tree", "HEAD-worktree"):
        sha = _try_resolve(root, ref)
        try:
            content = gs._run(["git", "show", f"{sha or ref}:{path}"], root)
            return FileContent(path=path, ref=sha or ref, content=content,
                               lines=len(content.splitlines()))
        except gs.GitError as e:
            raise ServiceError(f"файл не найден в {ref}: {path} ({e})") from e
    target = root / p
    if not target.exists():
        return FileContent(path=path, ref="working-tree", content="", lines=0, exists=False)
    text = target.read_text(encoding="utf-8", errors="replace")
    limit = s.max_chunk_lines * 40  # грубая защита от гигантских файлов
    trunc = len(text) > limit
    return FileContent(
        path=path,
        ref="working-tree",
        content=text[:limit],
        lines=len(text.splitlines()),
        truncated=trunc,
    )


def search_codebase(root: Path, query: str, s: Settings, limit: int = 20) -> list[dict]:
    if gs.is_git_repo(root):
        try:
            out = gs._run(
                ["git", "grep", "-n", "-I", "--no-color", "-e", query, "--", "."], root
            )
        except gs.GitError:
            out = ""
        hits = []
        for line in out.splitlines()[:limit]:
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            if m:
                hits.append({"file": m.group(1), "line": int(m.group(2)), "text": m.group(3)[:300]})
        return hits
    hits = []
    ql = query.lower()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in {".py", ".md", ".rst", ".toml"}:
            continue
        rel = p.relative_to(root).as_posix()
        if any(x in rel.split("/") for x in {".git", "__pycache__", ".venv"}):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if ql in line.lower():
                hits.append({"file": rel, "line": i, "text": line.strip()[:300]})
                if len(hits) >= limit:
                    return hits
    return hits


def bootstrap_plan(root: Path, s: Settings) -> BootstrapPlan:
    rev = gs.revision_from_repo(root) if gs.is_git_repo(root) else gs.revision_from_dir(root)
    return bootstrap_scanner.build_plan(
        rev.files, docs_dir=s.docs_dir, max_chunk_lines=s.max_chunk_lines
    )
