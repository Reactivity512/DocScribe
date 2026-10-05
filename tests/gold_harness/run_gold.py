"""Golden-харнес MCP-сервера (шаг 1) на gold-сете шага 0 — актуальная схема `gold-v1`.

Кейсов сейчас 44 (не 20, как в первой версии харнеса): 20 write_docs,
19 stay_silent, 5 escalate_bootstrap. Харнес работает со **схемой gold-v1**
(`expected_behavior` / `trigger_kind` / `severity` на верхнем уровне кейса,
diff лежит в `code_change.diff`), а не с устаревшим полем `expected`.

Что меряется (Definition of Done шага 1):
  * behavior accuracy + confusion 3x3 (write_docs / stay_silent / escalate_bootstrap);
  * trigger coverage по всем ожидаемым trigger_kind;
  * FP-rate на negative-кейсах (то, что бесит лида больше всего — шум в PR);
  * target coverage: куда агент собирается писать (readme/api-reference/adr/guide/changelog);
  * severity agreement (вспомогательная метрика, не гейт).

Запуск полностью офлайн и детерминирован (LLM не вызывается):
    python tests/gold_harness/run_gold.py [--report docs/reports/step1_gold_metrics.json]

Bootstrap-кейсы (`escalate_bootstrap`) содержат не unified diff, а «снимок сканера»
(`# bootstrap scan (not a diff)`). Детектор обязан распознать это и вернуть
escalate_bootstrap, а не should_update=True по мусорному разбору — см.
`docagent.mcp_server.analyzers.bootstrap_text`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from docagent.config import Settings  # noqa: E402
from docagent.mcp_server import service as svc  # noqa: E402

GOLD = REPO / "data" / "gold"
BEHAVIORS = ("write_docs", "stay_silent", "escalate_bootstrap")


# --------------------------------------------------------------------------- #
# загрузка
# --------------------------------------------------------------------------- #
def load_cases() -> list[dict]:
    cases = []
    for p in sorted((GOLD / "cases").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_id"] = p.stem
        cases.append(d)
    return cases


CASES = load_cases()
SETTINGS = Settings(docs_backend="fake")  # без LLM — чисто детерминированный прогон


# --------------------------------------------------------------------------- #
# предсказание поведения из DocsDecision
# --------------------------------------------------------------------------- #
def predict(case: dict):
    """Возвращает (decision, predicted_behavior)."""
    diff_text = case["code_change"]["diff"]
    decision = svc.decide_on_diff(diff_text, SETTINGS)
    if decision.behavior == "escalate_bootstrap":
        pred = "escalate_bootstrap"
    elif decision.should_update:
        pred = "write_docs"
    else:
        pred = "stay_silent"
    return decision, pred


def expected_targets(case: dict) -> list[str]:
    from docagent.mcp_server.analyzers.triggers import _target_for_file

    out = []
    for t in case["docs_change"].get("target_files") or []:
        v = _target_for_file(t).value
        if v not in out:
            out.append(v)
    return out


PRED = {c["_id"]: predict(c) for c in CASES}
EXP_TGT = {c["_id"]: expected_targets(c) for c in CASES}


# --------------------------------------------------------------------------- #
# метрики
# --------------------------------------------------------------------------- #
def report() -> dict:
    by_id = {c["_id"]: c for c in CASES}
    conf = Counter()
    for c in CASES:
        conf[(c["expected_behavior"], PRED[c["_id"]][1])] += 1

    pos = [c for c in CASES if c["expected_behavior"] == "write_docs"]
    neg = [c for c in CASES if c["expected_behavior"] == "stay_silent"]
    boot = [c for c in CASES if c["expected_behavior"] == "escalate_bootstrap"]

    tp = sum(conf[("write_docs", "write_docs")] for _ in [0])
    fn = len(pos) - conf[("write_docs", "write_docs")]
    fp = len([c for c in neg + boot if PRED[c["_id"]][1] == "write_docs"])
    tn = conf[("stay_silent", "stay_silent")]

    # trigger coverage (по всем кейсам, где есть ожидаемые триггеры != none)
    trig_total = trig_hit = 0
    per_trigger: dict[str, list[str]] = defaultdict(list)
    for c in CASES:
        exp = {t for t in c["trigger_kind"] if t != "none"}
        if not exp:
            continue
        got = {t.value for t in PRED[c["_id"]][0].trigger_kinds}
        for t in exp:
            trig_total += 1
            hit = t in got
            trig_hit += int(hit)
            per_trigger[t].append(("+" if hit else "-") + c["_id"])

    # target coverage (только там, где ожидание непустое)
    tgt_total = tgt_hit = 0
    tgt_miss: dict[str, list[str]] = defaultdict(list)
    for c in CASES:
        exp = EXP_TGT[c["_id"]]
        if not exp:
            continue
        got = {t.value for t in PRED[c["_id"]][0].expected_docs_targets}
        for t in exp:
            tgt_total += 1
            if t in got:
                tgt_hit += 1
            else:
                tgt_miss[t].append(c["_id"])

    sev_total = sev_hit = 0
    for c in CASES:
        if c["expected_behavior"] == "stay_silent":
            continue
        sev_total += 1
        sev_hit += int(PRED[c["_id"]][0].severity == c.get("severity", "medium"))

    acc = sum(conf[(b, b)] for b in BEHAVIORS) / len(CASES)
    return {
        "n": len(CASES),
        "counts": {"write_docs": len(pos), "stay_silent": len(neg), "escalate_bootstrap": len(boot)},
        "behavior_accuracy": round(acc, 4),
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(conf.items())},
        "binary": {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else 0.0,
            "recall_write_docs": round(tp / len(pos), 4) if pos else 0.0,
            "fp_rate_negative": round(fp / (len(neg) + len(boot)), 4) if (neg or boot) else 0.0,
        },
        "bootstrap_recall": round(conf[("escalate_bootstrap", "escalate_bootstrap")] / len(boot), 4)
        if boot else None,
        "trigger_coverage": round(trig_hit / trig_total, 4) if trig_total else 0.0,
        "trigger_total": trig_total,
        "per_trigger": {k: "".join(v) for k, v in sorted(per_trigger.items())},
        "target_coverage": round(tgt_hit / tgt_total, 4) if tgt_total else 0.0,
        "target_total": tgt_total,
        "target_misses": {k: v for k, v in sorted(tgt_miss.items())},
        "severity_agreement": round(sev_hit / sev_total, 4) if sev_total else 0.0,
        "errors": [
            {
                "id": c["_id"],
                "category": c["category"],
                "expected": c["expected_behavior"],
                "got": PRED[c["_id"]][1],
                "exp_triggers": c["trigger_kind"],
                "got_triggers": [t.value for t in PRED[c["_id"]][0].trigger_kinds],
                "ignore": [i.value for i in PRED[c["_id"]][0].ignore_reasons],
                "rules": PRED[c["_id"]][0].rules_fired,
                "exp_targets": EXP_TGT[c["_id"]],
                "got_targets": [t.value for t in PRED[c["_id"]][0].expected_docs_targets],
                "needs_llm": PRED[c["_id"]][0].needs_llm_verification,
                "evidence": [e.detail[:160] for e in PRED[c["_id"]][0].evidence][:3],
            }
            for c in CASES
            if PRED[c["_id"]][1] != c["expected_behavior"]
        ],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=None, help="куда положить JSON-метрики")
    ap.add_argument("--show-errors", action="store_true", default=True)
    args = ap.parse_args()

    r = report()
    print(json.dumps({k: v for k, v in r.items() if k != "errors"}, ensure_ascii=False, indent=2))
    if args.show_errors:
        print("\n=== MISCLASSIFIED ===")
        for e in r["errors"]:
            print(f"\n{e['id']} [{e['category']}] expected={e['expected']} got={e['got']}")
            print(f"   triggers exp={e['exp_triggers']} got={e['got_triggers']}")
            print(f"   ignore={e['ignore']} rules={e['rules']} needs_llm={e['needs_llm']}")
            print(f"   targets exp={e['exp_targets']} got={e['got_targets']}")
            for d in e["evidence"]:
                print(f"   ev: {d}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport -> {args.report}")
