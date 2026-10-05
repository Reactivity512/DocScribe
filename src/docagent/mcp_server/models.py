"""Модели данных (Pydantic) для контрактов MCP-инструментов шага 1.

Здесь описаны ТОЧНО те структуры, что возвращают инструменты.
Схема зафиксирована в docs/plan/STEP1_mcp_server.md и в README сервера.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Триггеры (spec §6 плана шага 0 → инструмент should_update_docs)
# --------------------------------------------------------------------------- #
class TriggerKind(str, Enum):
    api_new = "api_new"
    api_signature_changed = "api_signature_changed"
    api_removed = "api_removed"
    behavior_changed = "behavior_changed"
    adr_new = "adr_new"
    adr_changed = "adr_changed"
    dependency_added = "dependency_added"
    config_or_env_changed = "config_or_env_changed"
    breaking_change = "breaking_change"
    new_user_feature = "new_user_feature"
    none = "none"


class IgnoreReason(str, Enum):
    typo_only = "typo_only"
    ci_test_only = "ci_test_only"
    style_format_only = "style_format_only"
    docs_only = "docs_only"
    internal_refactor = "internal_refactor"
    no_public_api_impact = "no_public_api_impact"
    empty_diff = "empty_diff"
    diff_too_large = "diff_too_large"


class ExpectedDocsTarget(str, Enum):
    """Куда агенту писать — соответствует категориям gold-сета."""

    readme = "readme"
    api_reference = "api-reference"
    adr = "adr"
    guide = "guide"
    changelog = "changelog"


class Evidence(BaseModel):
    file: str
    symbol: str | None = None
    detail: str = ""


class DecisionBehavior(str, Enum):
    """Три состояния, совпадающие с `expected_behavior` схемы gold-v1."""

    write_docs = "write_docs"
    stay_silent = "stay_silent"
    escalate_bootstrap = "escalate_bootstrap"


class DocsDecision(BaseModel):
    should_update: bool
    trigger_kinds: list[TriggerKind] = Field(default_factory=list)
    ignore_reasons: list[IgnoreReason] = Field(default_factory=list)
    confidence: float = 0.0
    severity: str = "medium"
    expected_docs_targets: list[ExpectedDocsTarget] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    rules_fired: list[str] = Field(default_factory=list)
    needs_llm_verification: bool = False
    notes: str = ""
    behavior: DecisionBehavior = DecisionBehavior.stay_silent
    bootstrap_stage: int | None = None
    bootstrap_missing: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Публичный API (AST-эвристика: _-префикс + __all__)
# --------------------------------------------------------------------------- #
class SignatureParam(BaseModel):
    name: str
    annotation: str | None = None
    default: str | None = None


class SymbolInfo(BaseModel):
    kind: str  # function | class | method
    name: str
    qualified_name: str
    file: str
    line: int
    public: bool
    in_all: bool | None = None
    signature: str = ""
    params: list[SignatureParam] = Field(default_factory=list)
    returns: str | None = None
    has_docstring: bool = False
    decorators: list[str] = Field(default_factory=list)
    bases: list[str] = Field(default_factory=list)


class ApiSnapshot(BaseModel):
    """Публичный API одного ревизия репозитория."""

    ref: str
    symbols: list[SymbolInfo] = Field(default_factory=list)
    module_docstrings: dict[str, str] = Field(default_factory=dict)


class ApiChangeKind(str, Enum):
    added = "added"
    removed = "removed"
    changed = "changed"
    unchanged = "unchanged"


class ApiChange(BaseModel):
    kind: ApiChangeKind
    symbol: str
    file: str
    old_signature: str | None = None
    new_signature: str | None = None
    param_diff: dict = Field(default_factory=dict)
    breaking: bool = False
    reasons: list[str] = Field(default_factory=list)


class ApiDiffResult(BaseModel):
    before_ref: str
    after_ref: str
    changes: list[ApiChange] = Field(default_factory=list)
    breaking_count: int = 0


# --------------------------------------------------------------------------- #
# Diff / файлы
# --------------------------------------------------------------------------- #
class FileStat(BaseModel):
    path: str
    additions: int = 0
    deletions: int = 0
    is_doc: bool = False
    is_code: bool = False
    is_test: bool = False
    is_ci: bool = False
    is_config: bool = False
    language: str = "other"


class DiffStats(BaseModel):
    files: list[FileStat] = Field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    code_files: int = 0
    doc_files: int = 0
    test_files: int = 0
    ci_files: int = 0
    config_files: int = 0
    truncated: bool = False


class ChangedSymbol(BaseModel):
    name: str
    qualified_name: str
    file: str
    change: ApiChangeKind
    public: bool


class DiffAnalysis(BaseModel):
    summary: str
    stats: DiffStats
    changed_symbols: list[ChangedSymbol] = Field(default_factory=list)
    dependencies_added: list[str] = Field(default_factory=list)
    env_vars_touched: list[str] = Field(default_factory=list)
    doc_references: list[str] = Field(default_factory=list)
    hunk_headers: list[str] = Field(default_factory=list)
    text_deltas: dict = Field(default_factory=dict)
    parse_errors: list[str] = Field(default_factory=list)


class FileContent(BaseModel):
    path: str
    ref: str
    content: str
    lines: int
    truncated: bool = False
    exists: bool = True


# --------------------------------------------------------------------------- #
# Bootstrap-сканер (детерминированный, без LLM)
# --------------------------------------------------------------------------- #
class DocStatus(BaseModel):
    docs_exists: bool
    docs_dir: str
    has_readme: bool
    has_api_reference: bool
    has_adr: bool
    has_changelog: bool
    existing_files: list[str] = Field(default_factory=list)
    coverage_ratio: float = 0.0


class BootstrapStageDef(BaseModel):
    stage: int
    name: str
    title: str
    target_files: list[str] = Field(default_factory=list)
    chunks: list[dict] = Field(default_factory=list)
    rationale: str = ""


class BootstrapPlan(BaseModel):
    mode: str = "bootstrap"  # bootstrap | incremental
    status: DocStatus
    stages: list[BootstrapStageDef] = Field(default_factory=list)
    modules_scanned: int = 0
    public_symbols_total: int = 0
    notes: str = ""


# --------------------------------------------------------------------------- #
# Verify (LLM-надстройка над детерминированным диффом)
# --------------------------------------------------------------------------- #
class VerificationVerdict(str, Enum):
    user_visible = "user_visible"
    internal_only = "internal_only"
    uncertain = "uncertain"


class VerifyResult(BaseModel):
    verdict: VerificationVerdict
    user_visible: bool
    confidence: float
    reasoning: str = ""
    backend: str = "rule_fallback"
    model: str | None = None
    used_llm: bool = False


# --------------------------------------------------------------------------- #
# PR / контекст
# --------------------------------------------------------------------------- #
class PullRequestContext(BaseModel):
    repo: str
    number: int
    title: str
    body: str
    author: str
    source_branch: str
    target_branch: str
    head_sha: str
    base_sha: str
    labels: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    draft: bool = False
    merged: bool = False
    url: str | None = None
    fetched_at: str | None = None
    offline: bool = False
    error: str | None = None


class ToolError(BaseModel):
    error: str
    detail: str = ""
