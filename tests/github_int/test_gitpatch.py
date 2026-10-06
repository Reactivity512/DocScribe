"""Нормализация diff'ов gold-сета и проверка их применимости.

Gold-сет был сгенерирован с двумя дефектами формата, из-за которых `seed-pr`
не работал ни на одном кейсе:
  1. нет заголовков `diff --git a/… b/…` — сразу идёт `--- a/file` / `+++ b/file`,
     поэтому второй файл патча git считает мусором («corrupt patch at line N»);
  2. счётчики hunk-заголовков не соответствуют телу (45 кейсов из 49), а `git apply`
     требует точного совпадения.

Тесты проверяют три вещи: после нормализации счётчики сходятся, `git apply`
принимает патч к разбору (нет «corrupt patch»), и тело diff'а не изменилось.
Реальное применение патча к файлам проверяется в живом прогоне `seed-pr`: в
песочнице DSH `git apply` к незакоммиченным файлам возвращает «Skipped patch»
и ничего не делает, поэтому как проверка это непригодно.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import pytest  # noqa: E402

from docagent.github_int.gitpatch import (  # noqa: E402
    count_mismatches,
    has_git_header,
    normalize_patch,
    repair_patch,
)
from gold_cases import gold_diff_cases  # noqa: E402

WORK = REPO / "data" / "work"
CASES = gold_diff_cases()


def test_gold_set_is_nonempty():
    """Защита от молчаливого схлопывания параметризации в ноль."""
    assert len(CASES) >= 40, f"diff-кейсы не найдены: {len(CASES)}"


@pytest.mark.parametrize("case_id,diff", CASES, ids=[c[0] for c in CASES])
def test_normalized_patch_has_consistent_counters(case_id, diff):
    """Счётчики hunk'ов сходятся с телом и есть заголовок `diff --git`."""
    normalized = normalize_patch(diff)
    assert has_git_header(normalized), f"{case_id}: нет заголовка diff --git"
    assert count_mismatches(normalized) == [], \
        f"{case_id}: расхождения счётчиков: {count_mismatches(normalized)[:2]}"


@pytest.mark.parametrize("case_id,diff", CASES, ids=[c[0] for c in CASES])
def test_normalized_patch_is_accepted_by_git(case_id, diff):
    """`git apply --check` не должен ругаться на corrupt patch."""
    patch = WORK / f"pytest-{case_id}.patch"
    patch.write_text(normalize_patch(diff), encoding="utf-8")
    try:
        r = subprocess.run(["git", "-C", str(REPO), "apply", "--check",
                            "--whitespace=nowarn", str(patch)],
                           capture_output=True, text=True, timeout=60)
    finally:
        patch.unlink(missing_ok=True)
    assert "corrupt patch" not in (r.stderr or ""), \
        f"{case_id}: git считает патч повреждённым: {r.stderr.strip()[:200]}"


def test_new_file_hunk_gets_real_path():
    """Новый файл: `--- /dev/null` не должен попадать в заголовок патча."""
    patch = "--- /dev/null\n+++ b/demopkg/auth.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    out = normalize_patch(patch)
    assert "diff --git a/demopkg/auth.py b/demopkg/auth.py" in out
    assert "/dev/null" not in out.split("\n")[0]


def test_deleted_file_hunk_gets_real_path():
    patch = "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
    out = normalize_patch(patch)
    assert "diff --git a/old.py b/old.py" in out
    assert count_mismatches(out) == []


def test_repair_patch_rewrites_broken_header():
    """Ранняя версия нормализации писала `a//dev/null b//dev/null` — repair это лечит."""
    broken = ("diff --git a//dev/null b//dev/null\n--- /dev/null\n+++ b/x.py\n"
              "@@ -0,0 +1,1 @@\n+line\n")
    fixed = repair_patch(broken)
    assert fixed.startswith("diff --git a/x.py b/x.py")
    assert "//dev/null" not in fixed


def test_normalization_keeps_diff_body():
    """Нормализация меняет только заголовки, строки diff'а остаются как были."""
    _, diff = CASES[0]
    normalized = normalize_patch(diff)
    body = [ln for ln in normalized.splitlines()
            if not ln.startswith("diff --git ") and not ln.startswith("@@ ")]
    original_body = [ln for ln in diff.splitlines()
                     if not ln.startswith("diff --git ") and not ln.startswith("@@ ")]
    assert body == original_body


def test_normalization_is_idempotent():
    _, diff = CASES[0]
    once = normalize_patch(diff)
    assert normalize_patch(once) == once


def test_normalization_keeps_existing_headers():
    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    out = normalize_patch(patch)
    assert out.count("diff --git") == 1
    assert out.endswith("\n")


def test_multi_file_patch_gets_one_header_per_file():
    """Второй файл без заголовка — та самая причина «corrupt patch»."""
    patch = ("--- a/one.py\n+++ b/one.py\n@@ -1,1 +1,1 @@\n-a\n+A\n"
             "--- a/two.py\n+++ b/two.py\n@@ -1,1 +1,1 @@\n-b\n+B\n")
    out = normalize_patch(patch)
    assert out.count("diff --git") == 2
    assert "diff --git a/one.py b/one.py" in out
    assert "diff --git a/two.py b/two.py" in out
    assert count_mismatches(out) == []
