"""Общие помощники тестов gold-сета.

Сборщик diff-кейсов должен быть строгим: если фильтр вдруг перестанет匹配
(например, после починки формата), параметризованные тесты молча схлопнутся в
ноль и «пройдут» — это уже случалось. Поэтому здесь же живёт проверка, что набор
непустой.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GOLD = REPO / "data" / "gold" / "cases"

# не diff'ы: 5 кейсов с «снимком сканера» и 1 с текстовым сканом дерева
NON_DIFF_MARKERS = ("# bootstrap scan", "(no diff")


def load_gold_case(case_id: str) -> dict:
    return json.loads((GOLD / f"{case_id}.json").read_text(encoding="utf-8"))


def gold_diff_case_ids() -> list[str]:
    """id кейсов, у которых в code_change.diff лежит настоящий unified diff."""
    ids = []
    for p in sorted(GOLD.glob("*.json")):
        case = json.loads(p.read_text(encoding="utf-8"))
        diff = (case.get("code_change") or {}).get("diff") or ""
        head = diff.lstrip()
        if head.startswith(NON_DIFF_MARKERS) or not diff.strip():
            continue
        if head.startswith("diff --git") or head.startswith("--- "):
            ids.append(p.stem)
    return ids


def gold_diff_cases() -> list[tuple[str, str]]:
    out = []
    for cid in gold_diff_case_ids():
        case = load_gold_case(cid)
        out.append((cid, case["code_change"]["diff"]))
    return out
