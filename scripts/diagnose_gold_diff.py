"""Диагностика формата gold-diff'ов: есть ли заголовки `diff --git` (не тест)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CASES = sorted((REPO / "data" / "gold" / "cases").glob("*.json"))
stats = {"with_git_header": 0, "bare_pairs": 0, "not_diff": 0, "single_file": 0}
examples: list[str] = []

for p in CASES:
    case = json.loads(p.read_text(encoding="utf-8"))
    diff = (case.get("code_change") or {}).get("diff") or ""
    if not diff.strip():
        stats["not_diff"] += 1
        continue
    if diff.lstrip().startswith("# bootstrap scan"):
        stats["not_diff"] += 1
        continue
    has_git = bool(re.search(r"^diff --git ", diff, re.M))
    pairs = len(re.findall(r"^--- ", diff, re.M))
    if has_git:
        stats["with_git_header"] += 1
    else:
        stats["bare_pairs"] += 1
        if pairs == 1:
            stats["single_file"] += 1
        if len(examples) < 3:
            examples.append(f"{p.stem}: файлов={pairs}, первые строки: "
                            + " | ".join(diff.splitlines()[:3]))

print("кейсов:", len(CASES))
for k, v in stats.items():
    print(f"  {k}: {v}")
for e in examples:
    print("  пример:", e)

# проверка: сколько из них git apply --check проходит как есть
import subprocess  # noqa: E402

work = REPO / "data" / "work"
ok = bad = 0
for p in CASES[:12]:
    case = json.loads(p.read_text(encoding="utf-8"))
    diff = (case.get("code_change") or {}).get("diff") or ""
    if not diff.strip() or diff.lstrip().startswith("# bootstrap scan"):
        continue
    patch = work / f"fmt-{p.stem}.patch"
    patch.write_text(diff, encoding="utf-8")
    r = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", str(patch)],
                       capture_output=True, text=True, cwd=str(REPO))
    if r.returncode == 0:
        ok += 1
    else:
        bad += 1
    patch.unlink(missing_ok=True)
print(f"git apply --check (первые кейсы): ok={ok} bad={bad}")
