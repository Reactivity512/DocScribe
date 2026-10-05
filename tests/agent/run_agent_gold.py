"""Agent-harness (Step 2): run DocsAgent over all write_docs cases of the gold set.

Two modes:
  --backend fake   : deterministic draft generator, offline, for CI and structure checks;
  --backend ollama : real LLM via OpenAI-compat API (needs running Ollama).

Metrics:
  * drafts_produced_rate  — share of write_docs cases with >=1 valid draft;
  * hallucination_free    — share of produced drafts passing the anti-hallucination guard;
  * json_validity         — share of LLM calls whose reply parsed+validated on first attempt;
  * target_match          — share of cases where a draft targets the expected docs target;
  * must_include_hit      — share of positive cases whose draft contains >=1 must_include token
                            (meaningful only with a real LLM backend).

Usage:
  python tests/agent/run_agent_gold.py --backend fake [--report data/reports/step2_agent_fake.json]
  DOCAGENT_DOCS_BACKEND=ollama python tests/agent/run_agent_gold.py --backend ollama
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from docagent.config import Settings                       # noqa: E402
from docagent.agent.backends import FakeBackend, OpenAICompatBackend  # noqa: E402
from docagent.agent.loop import DocsAgent                  # noqa: E402
from docagent.agent.guards import check_hallucinated_symbols  # noqa: E402
from docagent.mcp_server import service as svc             # noqa: E402

GOLD = REPO / "data" / "gold"


def load_cases() -> list[dict]:
    cases = []
    for p in sorted((GOLD / "cases").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_id"] = p.stem
        cases.append(d)
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("fake", "ollama", "vllm"), default="fake")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    settings = Settings(docs_backend=args.backend if args.backend != "fake" else "fake")
    backend = FakeBackend(settings) if args.backend == "fake" else OpenAICompatBackend(settings)

    cases = [c for c in load_cases() if c["expected_behavior"] == "write_docs"]
    if args.limit:
        cases = cases[: args.limit]

    rows = []
    t0 = time.monotonic()
    for c in cases:
        diff = c["code_change"]["diff"]
        agent = DocsAgent(settings=settings, repo_path=str(REPO / "fixtures" / "demopkg"),
                          backend=backend)
        res = agent.run(diff, lang=c.get("lang", "ru"))
        allowed = set(agent.tools.allowed_symbols(diff))

        exp_targets = {t.value for t in res.decision.expected_docs_targets} or \
                      {ct for ct in (c["docs_change"].get("target_files") or [])}
        must = c["docs_change"].get("must_include") or []

        hallu = [d.target.value for d in res.drafts if check_hallucinated_symbols(d, allowed)]
        text_all = "\n".join(d.content_md for d in res.drafts).lower()
        got_targets = {d.target.value for d in res.drafts}
        row = {
            "id": c["_id"],
            "category": c["category"],
            "behavior": res.decision.behavior.value,
            "drafts": len(res.drafts),
            "dropped": res.dropped,
            "llm_calls": res.llm_calls,
            "hallucinated": hallu,
            "target_expected": sorted(exp_targets),
            "target_got": sorted(got_targets),
            "target_match": bool(got_targets & exp_targets) if exp_targets else None,
            "must_include_hit": (any(m.lower() in text_all for m in must) if must else None),
        }
        rows.append(row)
        flag = "OK " if row["drafts"] and not hallu else ("SILENT" if row["behavior"] != "write_docs" else "BAD ")
        print(f"{flag}{row['id']}: drafts={row['drafts']} llm={row['llm_calls']} "
              f"targets={row['target_got']} must={row['must_include_hit']} drop={bool(row['dropped'])}")

    elapsed = round(time.monotonic() - t0, 1)
    n = len(rows)
    silent = [r for r in rows if r["behavior"] != "write_docs"]
    drafted = [r for r in rows if r["drafts"] > 0]
    metrics = {
        "backend": args.backend,
        "n_write_docs_cases": n,
        "seconds": elapsed,
        "silently_skipped_by_rules": len(silent),
        "drafts_produced_rate": round(len(drafted) / max(n - len(silent), 1), 4),
        "hallucination_free_rate": round(sum(1 for r in drafted if not r["hallucinated"]) / max(len(drafted), 1), 4),
        "target_match_rate": round(sum(1 for r in drafted if r["target_match"]) / max(len(drafted), 1), 4),
        "must_include_hit_rate": round(
            sum(1 for r in drafted if r["must_include_hit"]) /
            max(sum(1 for r in drafted if r["must_include_hit"] is not None), 1), 4),
        "dropped_total": sum(len(r["dropped"]) for r in rows),
    }
    print("\n=== AGENT GOLD METRICS ===")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"metrics": metrics, "rows": rows}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"report -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
