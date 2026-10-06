"""Починка gold-сета: приводим diff'ы к формату, который принимает `git apply`.

Что делается с каждым `data/gold/cases/*.json`:
  * добавляются заголовки `diff --git a/… b/…`, если их нет;
  * пересчитываются счётчики hunk'ов (`git apply --recount`), потому что сет
    сгенерирован с заявленными, но неверными числами.

Содержимое строк diff'а не меняется — метрики шага 1 (детектор читает только
контекст hunk'а) не затрагиваются; это проверяется повторным прогоном харнесов.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from docagent.github_int.gitpatch import (  # noqa: E402
    count_mismatches,
    repair_patch,
)

CASES = REPO / "data" / "gold" / "cases"
dry_run = "--apply" not in sys.argv

changed = skipped = clean = 0
for p in sorted(CASES.glob("*.json")):
    case = json.loads(p.read_text(encoding="utf-8"))
    diff = (case.get("code_change") or {}).get("diff") or ""
    head = diff.lstrip()
    if not (head.startswith("--- ") or head.startswith("diff --git")):
        skipped += 1
        continue
    problems = count_mismatches(diff)
    fixed = repair_patch(diff)
    if fixed == diff:
        clean += 1
        continue
    changed += 1
    print(f"{p.stem}: правок счётчиков {len(problems)}"
          f"{' (dry-run)' if dry_run else ''}")
    if not dry_run:
        case["code_change"]["diff"] = fixed
        # без хвостового \n: так же, как файлы были записаны изначально
        p.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\nвсего кейсов: {changed + clean + skipped}")
print(f"  требуют починки: {changed}")
print(f"  уже корректны: {clean}")
print(f"  не diff (bootstrap/скан): {skipped}")
print("режим:", "dry-run (запусти с --apply, чтобы записать)" if dry_run else "изменения записаны")
