"""Правила триггеров: когда агент пишет документацию, а когда молчит.

Это реализация spec §6 плана шага 0 и одновременно контракт для Eval
(Trigger precision/recall на gold-сете). Всё детерминировано, LLM здесь не участвует —
LLM подключается только в verify_api_change (шаг 2 агента).

Важно про negative-кейсы `internal_refactor`: правило срабатывает по дельте
**токенов кода**, а не по изменённым строкам. Чистый rename локальной переменной
или перестановка строк не меняют множество токенов → молчим. Добавление новой
строки с новым идентификатором (например `_ = range(len(x))`) токены меняет →
правило НЕ игнорирует, дальше решает анализ публичного API. Это сознательный
выбор в пользу recall (см. docs/plan/STEP1_mcp_server.md, §4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docagent.mcp_server.analyzers.diff_parser import (
    DiffParse,
    ParsedFile,
    is_ci_path,
    is_code_path,
    is_config_path,
    is_doc_path,
    is_test_path,
    multiset_delta,
    tokens,
)
from docagent.mcp_server.analyzers import text_signals
from docagent.mcp_server.models import (
    DecisionBehavior,
    DocsDecision,
    Evidence,
    ExpectedDocsTarget,
    IgnoreReason,
    TriggerKind,
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass
class Signal:
    """Промежуточный результат анализа diff перед сводкой в решение."""

    kind: TriggerKind | None = None
    rule: str = ""
    file: str = ""
    symbol: str | None = None
    detail: str = ""
    severity: str = "medium"
    target: ExpectedDocsTarget | None = None
    ignore: IgnoreReason | None = None
    confidence: float = 0.9
    needs_llm: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Классификация файлов PR
# --------------------------------------------------------------------------- #
def classify_files(parse: DiffParse) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "code": [], "doc": [], "test": [], "ci": [], "config": [], "other": [],
    }
    for f in parse.files:
        p = f.path
        if is_doc_path(p):
            buckets["doc"].append(p)
        elif is_test_path(p):
            buckets["test"].append(p)
        elif is_ci_path(p):
            buckets["ci"].append(p)
        elif is_code_path(p):
            buckets["code"].append(p)
        elif is_config_path(p):
            buckets["config"].append(p)
        else:
            buckets["other"].append(p)
    return buckets


# --------------------------------------------------------------------------- #
# IGNORE-правила (порядок важен: первый совпавший доминирует)
# --------------------------------------------------------------------------- #
def _all_lines(fs: list[ParsedFile]) -> tuple[list[str], list[str]]:
    added = [l for f in fs for l in f.added_lines]
    removed = [l for f in fs for l in f.removed_lines]
    return added, removed


def detect_typo_only(parse: DiffParse, max_changed_lines: int, min_new_words: int) -> Signal | None:
    """Правки только орфографии/пунктуации в тексте, без новых словесных единиц."""
    code = [f for f in parse.files if is_code_path(f.path)]
    text = [f for f in parse.files if is_doc_path(f.path) or not is_code_path(f.path)]
    if code or not text:
        return None
    added, removed = _all_lines(text)
    changed = sum(f.additions + f.deletions for f in text)
    if changed == 0 or changed > max_changed_lines:
        return None
    delta = multiset_delta(added, removed)
    # новые слова есть → это уже не typo
    if delta["new_total"] >= min_new_words:
        return None
    # потерянные слова тоже должны быть единичными (иначе удалили смысловой кусок)
    if delta["lost_total"] >= min_new_words:
        return None
    return Signal(
        ignore=IgnoreReason.typo_only,
        rule="R-ignore-typo",
        file=text[0].path,
        detail=f"изменено строк: {changed}, новых слов: {delta['new_total']}",
        confidence=0.95,
        notes=["typo/punct-only правка документации"],
    )


def detect_style_format(parse: DiffParse) -> Signal | None:
    """Только форматирование/линт: множество токенов кода идентично."""
    code = [f for f in parse.files if is_code_path(f.path)]
    if not code:
        return None
    added, removed = _all_lines(code)
    if not added and not removed:
        return None
    a_toks, r_toks = tokens(added, removed)
    if a_toks != r_toks:
        return None
    non_ws_added = [l.strip() for l in added if l.strip()]
    non_ws_removed = [l.strip() for l in removed if l.strip()]
    if sorted(non_ws_added) == sorted(non_ws_removed):
        detail = "идентичные строки, изменились только отступы/переносы"
    else:
        detail = "множество токенов идентично (reformat/reorder)"
    other = [f.path for f in parse.files if not is_code_path(f.path)]
    return Signal(
        ignore=IgnoreReason.style_format_only,
        rule="R-ignore-style",
        file=code[0].path,
        detail=detail + (f"; тронуты не-код файлы: {other}" if other else ""),
        confidence=0.93,
    )


def detect_ci_test_only(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    touched = buckets["ci"] + buckets["test"]
    substantive = [
        f.path for f in parse.files
        if f.path in touched and (f.additions + f.deletions) > 0
    ]
    if not substantive:
        return None
    rest = [f.path for f in parse.files if f.path not in touched]
    if rest:
        return None
    return Signal(
        ignore=IgnoreReason.ci_test_only,
        rule="R-ignore-ci-test",
        file=substantive[0],
        detail=f"PR трогает только CI/тесты: {substantive}",
        confidence=0.97,
    )


def detect_docs_only(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    if not buckets["doc"]:
        return None
    non_doc = [f.path for f in parse.files if f.path not in buckets["doc"]]
    if non_doc:
        return None
    return Signal(
        ignore=IgnoreReason.docs_only,
        rule="R-ignore-docs-only",
        file=buckets["doc"][0],
        detail="изменена только документация — источник истины уже обновлён человеком",
        confidence=0.98,
    )


# --------------------------------------------------------------------------- #
# TRIGGER-правила
# --------------------------------------------------------------------------- #
_ADR_PATH_RE = re.compile(r"(^|/)(docs/)?(adr|decisions)/|ADR[-_]", re.I)
_DECISION_RE = re.compile(
    r"\b(architecture|архитектур\w*|decision record|ADR|stateless|безопасност\w*|"
    r"security model|storage backend|заменяет|replaces|migrat\w+)\b",
    re.I,
)


def detect_adr_signal(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    """Изменения в ADR-файлах или текст, явно описывающий архитектурное решение."""
    adr_files = [p for p in parse.by_path() if _ADR_PATH_RE.search(p)]
    if adr_files:
        existing = any(is_doc_path(p) for p in adr_files)
        return Signal(
            kind=TriggerKind.adr_changed if existing else TriggerKind.adr_new,
            rule="R-adr-file",
            file=adr_files[0],
            detail=f"затронуты ADR-файлы: {adr_files}",
            severity="high",
            target=ExpectedDocsTarget.adr,
            confidence=0.9,
        )
    hits: list[tuple[str, str]] = []
    for f in parse.files:
        if not (is_doc_path(f.path) or f.path.endswith(".md")):
            continue
        blob = "\n".join(f.added_lines)
        if len(_WORD_RE.findall(blob)) < 20:
            continue
        m = _DECISION_RE.search(blob)
        if m:
            hits.append((f.path, m.group(0)))
    if hits:
        path, kw = hits[0]
        return Signal(
            kind=TriggerKind.adr_new,
            rule="R-adr-text",
            file=path,
            detail=f"добавлен раздел с решением по архитектуре (маркер: {kw!r})",
            severity="high",
            target=ExpectedDocsTarget.adr,
            confidence=0.8,
            needs_llm=True,
        )
    return None


def detect_dependency_added(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    from docagent.mcp_server.analyzers.diff_parser import extract_added_dependencies

    deps = extract_added_dependencies(parse)
    if not deps:
        return None
    cfg = buckets["config"] or [f.path for f in parse.files]
    return Signal(
        kind=TriggerKind.dependency_added,
        rule="R-deps",
        file=cfg[0],
        detail=f"новые зависимости: {deps}",
        severity="medium",
        target=ExpectedDocsTarget.readme,
        confidence=0.9,
    )


_ENV_DEF_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*[:=]")


def detect_config_env(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    from docagent.mcp_server.analyzers.diff_parser import extract_env_vars

    env = set(extract_env_vars(parse))
    for f in parse.files:
        base = f.path.split("/")[-1]
        if base.startswith(".env") or base.endswith(("settings.py", "config.py")) or f.path in buckets["config"]:
            for line in f.added_lines:
                m = _ENV_DEF_RE.match(line)
                if m:
                    env.add(m.group(1))
    if not env:
        return None
    return Signal(
        kind=TriggerKind.config_or_env_changed,
        rule="R-config-env",
        file=(buckets["config"] or [parse.files[0].path])[0],
        detail=f"изменены переменные окружения/конфигурации: {sorted(env)}",
        severity="medium",
        target=ExpectedDocsTarget.readme,
        confidence=0.85,
    )


def breaking_signals_from_api(api_changes: list) -> list[Signal]:
    """Сигналы из детерминированного AST-диффа публичного API (golden source истины).

    Аргумент — сырые записи `ApiChangeRecord` (с флагом symbol_public), а не
    публичный `ApiChange`, который фильтруется по публичным символам.
    """
    out: list[Signal] = []
    for ch in api_changes:
        if ch.kind == "unchanged":
            continue
        if not ch.symbol_public:
            continue
        kind = {
            "added": TriggerKind.api_new,
            "removed": TriggerKind.api_removed,
            "changed": TriggerKind.api_signature_changed,
        }[ch.kind.value]
        out.append(
            Signal(
                kind=kind,
                rule=f"R-api-{ch.kind.value}",
                file=ch.file,
                symbol=ch.symbol,
                detail=_api_detail(ch),
                severity="high" if ch.breaking else "medium",
                target=ExpectedDocsTarget.api_reference,
                confidence=0.95,
                needs_llm=ch.kind.value == "changed",
            )
        )
    return out


def _api_detail(ch) -> str:
    bits = []
    if ch.old_signature and ch.new_signature:
        bits.append(f"{ch.old_signature} → {ch.new_signature}")
    elif ch.new_signature:
        bits.append(ch.new_signature)
    elif ch.old_signature:
        bits.append(f"удалён: {ch.old_signature}")
    pd = ch.param_diff or {}
    for key in ("added", "removed", "default_changed", "annotation_changed"):
        if pd.get(key):
            bits.append(f"{key}: {pd[key]}")
    if ch.breaking:
        bits.append("BREAKING: " + "; ".join(ch.reasons))
    return " | ".join(bits)


def internal_refactor_signal(parse: DiffParse, api_changes: list, buckets: dict[str, list[str]]) -> Signal | None:
    """Рефакторинг без изменения публичного API → молчим (если нет прочих сигналов).

    Guard'ы против FN (gold-009/016/049): правило НЕ игнорирует PR, если
      * изменены dunder-протоколы (__enter__/__exit__/__call__/...) — это
        пользовательский контракт (context manager и т.п.);
      * добавлены новые публичные def/class в diff;
      * изменён __all__ или реэкспорт из корня пакета (`from .x import y`) —
        это расширение публичной поверхности даже без AST-символов.
    """
    code = buckets["code"]
    if not code:
        return None
    if any(ch.symbol_public for ch in api_changes):
        return None
    by_path = parse.by_path()
    added_all: list[str] = []
    for p in code:
        added_all.extend(by_path[p].added_lines)
    # dunder-протоколы = контракт для пользователей
    if any(re.match(r"\s*(?:async\s+)?def\s+__(?:enter|exit|call|iter|next|aenter|aexit)__", l) for l in added_all):
        return None
    # новый публичный символ в тексте diff (AST мог его не смочь разобрать)
    if _has_public_symbol(parse, buckets):
        return None
    # реэкспорт / расширение __all__ из корня пакета
    root_init = [p for p in code if p.endswith("__init__.py") and p.count("/") <= 1]
    for p in root_init:
        f = by_path[p]
        blob_a = "\n".join(f.added_lines)
        blob_r = "\n".join(f.removed_lines)
        if "__all__" in blob_a and blob_a != blob_r:
            return None
        if re.search(r"^\s*from\s+\.\w+\s+import\s+", blob_a, re.M):
            return None
    added, removed = _all_lines([by_path[p] for p in code])
    a_toks, r_toks = tokens(added, removed)
    if a_toks == r_toks:
        return None  # это style_format, обработано раньше
    new_ident = {t for t in a_toks - r_toks if re.match(r"^[A-Za-z_]\w*$", t)}
    lost_ident = {t for t in r_toks - a_toks if re.match(r"^[A-Za-z_]\w*$", t)}
    if not (new_ident & set(_public_names(api_changes))):
        return Signal(
            ignore=IgnoreReason.internal_refactor,
            rule="R-ignore-refactor",
            file=code[0],
            detail=(
                f"публичный API не изменился; внутренние токены +/-: "
                f"{sorted(new_ident)[:6]} / {sorted(lost_ident)[:6]}"
            ),
            confidence=0.8,
            needs_llm=True,
        )
    return None


def _public_names(api_changes: list) -> list[str]:
    return [c.symbol for c in api_changes if c.symbol_public]


def _has_public_symbol(parse: DiffParse, buckets: dict[str, list[str]]) -> bool:
    """Есть ли в изменённых код-файлах новые публичные def/class (по тексту diff)."""
    pub_def = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([^\s_(,:]+)")
    for p in buckets["code"]:
        f = parse.by_path()[p]
        for line in f.added_lines:
            m = pub_def.match(line)
            if m and not m.group(1).startswith("_"):
                return True
    return False


# --------------------------------------------------------------------------- #
# Цели документации: куда писать по типу сигнала
# --------------------------------------------------------------------------- #
def _target_for_file(path: str) -> ExpectedDocsTarget:
    """README vs guide/api/changelog — по пути целевого файла документации."""
    low = path.lower()
    if low == "readme.md" or low.endswith("/readme.md"):
        return ExpectedDocsTarget.readme
    if "changelog" in low or low.endswith("history.md"):
        return ExpectedDocsTarget.changelog
    if "/adr/" in f"/{low}" or re.search(r"adr[-_]", low):
        return ExpectedDocsTarget.adr
    if "api-reference" in low or "api_reference" in low:
        return ExpectedDocsTarget.api_reference
    return ExpectedDocsTarget.guide


def _declared_doc_targets(parse: DiffParse) -> list[ExpectedDocsTarget]:
    """Документационные файлы, которые PR уже тронул: агент правит ту же цель."""
    out = []
    for f in parse.files:
        if is_doc_path(f.path) or f.path.lower() == "readme.md":
            t = _target_for_file(f.path)
            if t not in out:
                out.append(t)
    return out


# --------------------------------------------------------------------------- #
# TRIGGER-правила: второй слой (текстовые эвристики, см. text_signals)
# --------------------------------------------------------------------------- #
def feature_signal(parse: DiffParse) -> Signal | None:
    hit = text_signals.detect_new_user_feature(parse.files)
    if not hit:
        return None
    path, name, detail, score = hit
    conf = min(0.5 + 0.1 * score, 0.85)
    targets = _declared_doc_targets(parse) or [ExpectedDocsTarget.guide]
    return Signal(
        kind=TriggerKind.new_user_feature,
        rule="R-feature",
        file=path,
        symbol=name,
        detail=detail,
        severity="high" if score >= 5 else "medium",
        target=targets[0],
        confidence=conf,
        needs_llm=True,
        notes=["new_user_feature — правило-фолбэк, на шаге 2 решение отдаётся LLM"],
    )


def config_env_signal_v2(parse: DiffParse, buckets: dict[str, list[str]]) -> Signal | None:
    """Расширенная версия R-config-env: pydantic-настройки, ключи сборки, опции."""
    hit = text_signals.detect_config_env(parse.files)
    if not hit:
        return None
    path, names, detail = hit
    targets = _declared_doc_targets(parse)
    if not targets:
        targets = [ExpectedDocsTarget.readme]
    return Signal(
        kind=TriggerKind.config_or_env_changed,
        rule="R-config-env2",
        file=path or (buckets["config"] or [f.path for f in parse.files])[0],
        detail=f"{sorted(set(names))}: {detail}",
        severity="medium",
        target=targets[0],
        confidence=0.8,
        needs_llm=True,
    )


def behavior_extra_signal(parse: DiffParse) -> Signal | None:
    hit = text_signals.detect_behavior_extra(parse.files)
    if not hit:
        return None
    path, name, detail = hit
    targets = _declared_doc_targets(parse) or [ExpectedDocsTarget.guide]
    return Signal(
        kind=TriggerKind.behavior_changed,
        rule="R-behavior-extra",
        file=path,
        symbol=name,
        detail=detail,
        severity="medium",
        target=targets[0],
        confidence=0.8,
        needs_llm=True,
    )


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def summarize(
    parse: DiffParse,
    *,
    api_changes: list | None = None,
    has_behavior_delta: bool = False,
    behavior_detail: str = "",
    behavior_file: str = "",
    typo_max_changed_lines: int = 6,
    typo_min_new_words: int = 8,
    truncated: bool = False,
) -> DocsDecision:
    """Главная функция правил: diff (+опц. AST-дифф) → DocsDecision."""
    api_changes = api_changes or []
    buckets = classify_files(parse)
    signals: list[Signal] = []

    if not parse.files or all(not (f.additions or f.deletions) for f in parse.files):
        return DocsDecision(
            should_update=False,
            ignore_reasons=[IgnoreReason.empty_diff],
            confidence=1.0,
            rules_fired=["R-empty"],
            notes="пустой diff",
        )
    if truncated or parse.truncated:
        return DocsDecision(
            should_update=False,
            ignore_reasons=[IgnoreReason.diff_too_large],
            confidence=1.0,
            rules_fired=["R-too-large"],
            notes="diff обрезан по лимиту — анализ невозможен, нужен ручной разбор",
        )

    # --- IGNORE -------------------------------------------------------------
    ignores: list[IgnoreReason] = []
    for det in (
        lambda: detect_ci_test_only(parse, buckets),
        lambda: detect_docs_only(parse, buckets),
        lambda: detect_typo_only(parse, typo_max_changed_lines, typo_min_new_words),
        lambda: detect_style_format(parse),
    ):
        s = det()
        if s and s.ignore:
            ignores.append(s.ignore)
            signals.append(s)

    # --- TRIGGER ------------------------------------------------------------
    sigs = breaking_signals_from_api(api_changes)
    signals.extend(sigs)
    kinds = {s.kind for s in sigs if s.kind}

    if any(ch.breaking for ch in api_changes):
        kinds.add(TriggerKind.breaking_change)

    for det, extra in (
        (lambda: detect_adr_signal(parse, buckets), None),
        (lambda: detect_dependency_added(parse, buckets), TriggerKind.dependency_added),
        (lambda: detect_config_env(parse, buckets), TriggerKind.config_or_env_changed),
    ):
        s = det()
        if s and s.kind:
            kinds.add(s.kind)
            signals.append(s)

    # --- второй слой: текстовые эвристики ------------------------------------
    for det in (
        lambda: feature_signal(parse),
        lambda: config_env_signal_v2(parse, buckets),
        lambda: behavior_extra_signal(parse),
    ):
        s = det()
        if s and s.kind:
            kinds.add(s.kind)
            signals.append(s)

    if has_behavior_delta:
        kinds.add(TriggerKind.behavior_changed)
        signals.append(
            Signal(
                kind=TriggerKind.behavior_changed,
                rule="R-behavior",
                file=behavior_file or (buckets["code"] or [""])[0],
                detail=behavior_detail or "изменено поведение функции без смены сигнатуры",
                severity="medium",
                target=ExpectedDocsTarget.guide,
                confidence=0.85,
                needs_llm=True,
            )
        )

    # цели: сигнальные + те док-файлы, что PR уже тронул (README/guide/changelog)
    declared_targets = _declared_doc_targets(parse)

    triggers = sorted({k.value for k in kinds if k}, key=str)

    # если ничего не сработало — проверяем внутренний рефакторинг
    if not triggers and not ignores:
        s = internal_refactor_signal(parse, api_changes, buckets)
        if s and s.ignore:
            ignores.append(s.ignore)
            signals.append(s)

    if not triggers and not ignores:
        ignores.append(IgnoreReason.no_public_api_impact)
        signals.append(
            Signal(
                ignore=IgnoreReason.no_public_api_impact,
                rule="R-default-silent",
                file=(buckets["code"] or buckets["other"] or [""])[0],
                detail="ни одно триггер-правило не сработало",
                confidence=0.75,
            )
        )

    should = bool(triggers)
    target_set = {s.target.value for s in signals if s.target}
    target_set.update(t.value for t in declared_targets)
    targets = sorted(target_set)
    if should:
        if not targets:
            targets = [ExpectedDocsTarget.api_reference.value]
    severity = "medium"
    for s in signals:
        if s.kind and _SEVERITY_ORDER[s.severity] > _SEVERITY_ORDER[severity]:
            severity = s.severity

    evidence = [
        Evidence(file=s.file, symbol=s.symbol, detail=f"[{s.rule}] {s.detail}")
        for s in signals
        if s.file
    ]
    confidences = [s.confidence for s in signals]
    confidence = round(max(confidences) if confidences else 0.5, 2)

    return DocsDecision(
        should_update=should,
        behavior=DecisionBehavior.write_docs if should else DecisionBehavior.stay_silent,
        trigger_kinds=[TriggerKind(k) for k in triggers],
        ignore_reasons=ignores,
        confidence=confidence,
        severity=severity,
        expected_docs_targets=[ExpectedDocsTarget(t) for t in targets],
        evidence=evidence,
        rules_fired=sorted({s.rule for s in signals}),
        needs_llm_verification=any(s.needs_llm for s in signals),
        notes="; ".join(n for s in signals for n in s.notes)[:500],
    )
