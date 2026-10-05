#!/usr/bin/env python3
"""Валидатор gold-датасета (шаг 0).

Проверяет каждую запись data/gold/cases/*.json:
  1. соответствие JSON Schema (data/gold/schema.json);
  2. кросс-инварианты, которые неудобно выразить в схеме
     (баланс negative >= 30%, уникальность id, согласованность category
     и expected_behavior, must_include внутри эталонного текста,
     принадлежность target_files зоне docs/);
  3. сводную статистику по категориям / языкам / триггерам.

Использование:
    python scripts/validate_gold.py            # только отчёт
    python scripts/validate_gold.py --strict   # exit code != 0 при проблемах
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    sys.exit("Нужен пакет jsonschema: pip install jsonschema")

ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = ROOT / "data" / "gold"
CASES_DIR = GOLD_DIR / "cases"
RAW_DIR = GOLD_DIR / "raw"
SCHEMA_PATH = GOLD_DIR / "schema.json"

# Категории, в которых агент обязан молчать (anti-spam ядро gold-сета)
SILENT_CATEGORIES = {
    "typo_fix", "internal_refactor", "ci_test_only", "style_format_only", "docs_only",
}
REQUIRED_CATEGORIES = {
    "api_change", "feature_doc", "adr_impact", "readme_update", "typo_fix",
    "internal_refactor", "ci_test_only", "style_format_only", "docs_only", "bootstrap",
}
MIN_CASES = 30
MIN_NEGATIVE_RATIO = 0.30


def load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def schema_errors(case: dict, validator: Draft202012Validator) -> list[str]:
    out = []
    for err in sorted(validator.iter_errors(case), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"schema {loc}: {err.message}")
    return out


def semantic_errors(case: dict) -> list[str]:
    """Инварианты, которые не выражаются (или выражаются громоздко) в JSON Schema."""
    errs: list[str] = []
    beh = case.get("expected_behavior")
    cat = case.get("category")
    docs = case.get("docs_change", {})
    labels = set(case.get("labels", []))

    # negative <=> stay_silent
    if (beh == "stay_silent") != (cat in SILENT_CATEGORIES):
        errs.append(
            f"рассогласование: category={cat} не соответствует expected_behavior={beh}"
        )
    is_negative = beh == "stay_silent"
    if is_negative and "negative" not in labels:
        errs.append("у negative-кейса нет label 'negative'")
    if not is_negative and "negative" in labels:
        errs.append("label 'negative' на positive-кейсе")

    # файлы документации должны лежать в docs/ или быть README.md
    for tf in docs.get("target_files", []):
        if not (tf.startswith("docs/") or tf.endswith("README.md")):
            errs.append(f"target_file вне зоны документации: {tf}")

    # bootstrap требует пустого docs/
    if beh == "escalate_bootstrap":
        if cat != "bootstrap":
            errs.append("escalate_bootstrap возможен только для category=bootstrap")
        if case.get("source", {}).get("kind") != "bootstrap_scan":
            errs.append("bootstrap-кейс обязан иметь source.kind=bootstrap_scan")

    # must_include должен встречаться в эталоне, иначе метрика Faithfulness бесполезна.
    # Файловые пути (содержат "/" или ".md") проверяются по target_files, а не по тексту.
    text = docs.get("human_written_text") or ""
    targets = " ".join(docs.get("target_files", []))
    for token in docs.get("must_include", []):
        if not text:
            continue
        is_path_like = ("/" in token) or token.endswith(".md")
        haystack = f"{text}\n{targets}" if is_path_like else text
        if token not in haystack:
            errs.append(f"must_include '{token}' отсутствует в human_written_text/target_files")
    for token in docs.get("must_not_include", []):
        if text and token in text:
            errs.append(f"must_not_include '{token}' найден в human_written_text — кейс сломан")

    # diff должен быть похож на unified diff (кроме bootstrap-снимков)
    diff = case.get("code_change", {}).get("diff", "")
    if beh != "escalate_bootstrap" and "---" not in diff:
        errs.append("code_change.diff не похож на unified diff")

    # сырой патч опционален, но если заявлен в raw_patch — файл обязан существовать
    if case.get("raw_patch") and not (RAW_DIR / case["raw_patch"]).exists():
        errs.append(f"raw_patch отсутствует: data/gold/raw/{case['raw_patch']}")

    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate DocAgent gold dataset")
    ap.add_argument("--strict", action="store_true", help="exit 1 при любых проблемах")
    args = ap.parse_args()

    if not SCHEMA_PATH.exists():
        sys.exit(f"Нет схемы: {SCHEMA_PATH}")
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    files = sorted(CASES_DIR.glob("gold-*.json"))
    if not files:
        print(f"Папка {CASES_DIR} пуста — gold-сет не собран")
        return 1 if args.strict else 0

    cases: list[dict] = []
    total_errors = 0
    seen_ids: set[str] = set()

    for path in files:
        try:
            case = load_json(path)
        except json.JSONDecodeError as exc:
            print(f"FAIL {path.name}: битый JSON ({exc})")
            total_errors += 1
            continue

        errors = schema_errors(case, validator) + semantic_errors(case)
        cid = case.get("id", path.stem)
        if cid in seen_ids:
            errors.append(f"дубликат id={cid}")
        seen_ids.add(cid)
        cases.append(case)

        status = "OK  " if not errors else "FAIL"
        print(f"{status} {path.name} [{case.get('category')} -> {case.get('expected_behavior')}]")
        for e in errors:
            print(f"       - {e}")
        total_errors += len(errors)

    n = len(cases)
    neg = sum(1 for c in cases if c.get("expected_behavior") == "stay_silent")
    cats = Counter(c.get("category") for c in cases)
    langs = Counter(c.get("lang") for c in cases)
    kinds = Counter(c.get("source", {}).get("kind") for c in cases)
    trig = Counter(t for c in cases for t in c.get("trigger_kind", []))

    print("\n=== Сводка ===")
    print(f"Кейсов: {n} (минимум для MVP: {MIN_CASES})")
    ratio = neg / n if n else 0.0
    print(f"Negative (агент молчит): {neg} = {ratio:.0%} (минимум {MIN_NEGATIVE_RATIO:.0%})")
    print("Категории:")
    for k, v in cats.most_common():
        print(f"  {v:>2}  {k}")
    print(f"Языки: {dict(langs)}")
    print(f"Источник: {dict(kinds)}")
    print(f"Триггеры: {dict(trig)}")

    problems = []
    if n < MIN_CASES:
        problems.append(f"мало кейсов: {n} < {MIN_CASES}")
    if n and ratio < MIN_NEGATIVE_RATIO:
        problems.append(f"доля negative ниже {MIN_NEGATIVE_RATIO:.0%}: {neg}/{n}")
    absent = REQUIRED_CATEGORIES - set(cats)
    if absent:
        problems.append("нет кейсов категорий: " + ", ".join(sorted(absent)))
    ru = langs.get("ru", 0)
    en = langs.get("en", 0)
    if ru == 0 or en == 0:
        problems.append("нужны кейсы на обоих языках (ru и en)")
    if total_errors:
        problems.append(f"ошибок в записях: {total_errors}")

    print("\n=== Вердикт ===")
    if problems:
        for p in problems:
            print(f"  [X] {p}")
    else:
        print("  [OK] Gold-сет валиден и сбалансирован")

    return 1 if (problems and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
