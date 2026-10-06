"""Eval (шаг 6): gold-регрессия одним отчётом-гейтом.

Зачем: после шагов 4-5 изменился маршрут графа и публикация, а правила/промпты
могут «поехать» от любой правки. Гейт на gold-сете — единственная дешёвая
страховка, что мы не откатили качество.

Две части:
  * rules  — детерминированный харнес шага 1 (MCP-правила, без LLM);
  * agent  — харнес шага 2 (генерация черновиков, backend по умолчанию fake).

Функции переиспользуют существующие харнесы, а не копируют их: иначе метрики
гейта и метрики шагов 1-2 разъедутся.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ пороги гейта --
@dataclass(frozen=True)
class Gates:
    """Пороги, откалиброванные по факту прогонов (см. data/reports/eval_gold.*).

    Занижены осознанно: гейт ловит регресс, а не требует идеала. Известные
    ограничения шага 1 (trigger/target coverage) не гейтятся — иначе гейт был бы
    всегда красным и его перестали бы читать.
    """
    rules_behavior_accuracy: float = 0.85
    rules_fp_rate_negative: float = 0.05      # FP на negative — главный раздражитель лида
    agent_drafts_produced_rate: float = 0.60
    agent_target_match_rate: float = 0.80


@dataclass
class EvalReport:
    rules: dict = field(default_factory=dict)
    agent: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "rules": self.rules, "agent": self.agent,
                "checks": self.checks, "errors": self.errors}


def _load_module(name: str, path: Path):
    """Импорт существующего харнеса по пути (tests/ не является пакетом)."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------ прогоны --
def run_rules(cases_limit: int | None = None) -> dict:
    """Метрики детерминированных правил (шаг 1) на gold-сете."""
    mod = _load_module("gold_rules_harness", REPO / "tests" / "gold_harness" / "run_gold.py")
    cases = mod.CASES if cases_limit is None else mod.CASES[:cases_limit]
    conf: dict = {}
    for c in cases:
        key = (c["expected_behavior"], mod.PRED[c["_id"]][1])
        conf[key] = conf.get(key, 0) + 1
    behaviors = ("write_docs", "stay_silent", "escalate_bootstrap")
    neg = [c for c in cases if c["expected_behavior"] in ("stay_silent", "escalate_bootstrap")]
    fp = sum(1 for c in neg if mod.PRED[c["_id"]][1] == "write_docs")
    full = mod.report()
    return {
        "n": len(cases),
        "behavior_accuracy": round(sum(conf.get((b, b), 0) for b in behaviors) / max(len(cases), 1), 4),
        "fp_rate_negative": round(fp / max(len(neg), 1), 4),
        "bootstrap_recall": full.get("bootstrap_recall"),
        "trigger_coverage": full.get("trigger_coverage"),
        "target_coverage": full.get("target_coverage"),
        "counts": full.get("counts"),
        "misclassified": [e["id"] for e in full.get("errors", [])],
    }


def run_agent(backend: str = "fake", cases_limit: int | None = None) -> dict:
    """Метрики агента (шаг 2) на write-кейсах gold-сета."""
    mod = _load_module("gold_agent_harness", REPO / "tests" / "agent" / "run_agent_gold.py")
    from docagent.agent.backends import FakeBackend, OpenAICompatBackend
    from docagent.agent.guards import check_hallucinated_symbols
    from docagent.agent.loop import DocsAgent
    from docagent.config import Settings

    settings = Settings(docs_backend="fake" if backend == "fake" else backend)
    be = FakeBackend(settings) if backend == "fake" else OpenAICompatBackend(settings)
    cases = [c for c in mod.load_cases() if c["expected_behavior"] == "write_docs"]
    if cases_limit:
        cases = cases[:cases_limit]

    rows = []
    for c in cases:
        diff = c["code_change"]["diff"]
        agent = DocsAgent(settings=settings,
                          repo_path=str(REPO / "fixtures" / "demopkg"), backend=be)
        res = agent.run(diff, lang=c.get("lang", "ru"))
        allowed = set(agent.tools.allowed_symbols(diff))
        exp = {t.value for t in res.decision.expected_docs_targets} or \
              set(c["docs_change"].get("target_files") or [])
        must = c["docs_change"].get("must_include") or []
        text_all = "\n".join(d.content_md for d in res.drafts).lower()
        got = {d.target.value for d in res.drafts}
        rows.append({
            "id": c["_id"], "category": c["category"],
            "behavior": res.decision.behavior.value,
            "drafts": len(res.drafts), "dropped": res.dropped,
            "hallucinated": [d.target.value for d in res.drafts
                             if check_hallucinated_symbols(d, allowed)],
            "target_expected": sorted(exp), "target_got": sorted(got),
            "target_match": bool(got & exp) if exp else None,
            "must_include_hit": (any(m.lower() in text_all for m in must) if must else None),
        })

    n = len(rows)
    drafted = [r for r in rows if r["drafts"]]
    m = {
        "backend": backend,
        "n_write_docs_cases": n,
        "drafts_produced_rate": round(len(drafted) / max(n, 1), 4),
        "hallucination_free_rate": round(sum(1 for r in drafted if not r["hallucinated"])
                                         / max(len(drafted), 1), 4),
        "target_match_rate": round(sum(1 for r in drafted if r["target_match"])
                                   / max(len(drafted), 1), 4),
        "must_include_hit_rate": round(
            sum(1 for r in drafted if r["must_include_hit"]) /
            max(sum(1 for r in drafted if r["must_include_hit"] is not None), 1), 4),
        "dropped_total": sum(len(r["dropped"]) for r in rows),
        "rows": rows,
    }
    return m


# --------------------------------------------------------------------------- гейт --
def _check(name: str, value, ok: bool, detail: str = "") -> dict:
    return {"name": name, "value": value, "ok": bool(ok), "detail": detail}


_KEEP = object()  # «не задан»: rules_limit=None означает «весь сет», а не «как limit»


def evaluate(backend: str = "fake", limit: int | None = None,
             gates: Gates | None = None, rules_limit=_KEEP) -> EvalReport:
    """`limit` ограничивает обе части, `rules_limit` — только правила (быстрые).

    Разделение нужно потому, что правила считаются мгновенно, а агент — секунды
    на кейс: гейт по правилам можно гонять на полном сете, не ожидая агента.
    """
    g = gates or Gates()
    rep = EvalReport()
    try:
        rep.rules = run_rules(limit if rules_limit is _KEEP else rules_limit)
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"rules harness: {type(e).__name__}: {e}")
    try:
        rep.agent = run_agent(backend, limit)
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"agent harness: {type(e).__name__}: {e}")

    if rep.rules:
        rep.checks.append(_check(
            "rules.behavior_accuracy", rep.rules["behavior_accuracy"],
            rep.rules["behavior_accuracy"] >= g.rules_behavior_accuracy,
            f"порог ≥ {g.rules_behavior_accuracy}"))
        rep.checks.append(_check(
            "rules.fp_rate_negative", rep.rules["fp_rate_negative"],
            rep.rules["fp_rate_negative"] <= g.rules_fp_rate_negative,
            f"порог ≤ {g.rules_fp_rate_negative} (шум в PR на negative-кейсах)"))
    if rep.agent:
        rep.checks.append(_check(
            "agent.drafts_produced_rate", rep.agent["drafts_produced_rate"],
            rep.agent["drafts_produced_rate"] >= g.agent_drafts_produced_rate,
            f"порог ≥ {g.agent_drafts_produced_rate}"))
        rep.checks.append(_check(
            "agent.target_match_rate", rep.agent["target_match_rate"],
            rep.agent["target_match_rate"] >= g.agent_target_match_rate,
            f"порог ≥ {g.agent_target_match_rate}"))
    return rep


# -------------------------------------------------------------------------- отчёт --
def render_markdown(rep: EvalReport) -> str:
    lines = ["# Eval — gold-регрессия (шаг 6)", ""]
    lines.append(f"**Итог: {'PASS' if rep.passed else 'FAIL'}**")
    lines.append("")
    lines.append("| Проверка | Значение | Порог | Статус |")
    lines.append("|---|---|---|---|")
    for c in rep.checks:
        lines.append(f"| `{c['name']}` | {c['value']} | {c['detail']} | "
                     f"{'✅' if c['ok'] else '❌'} |")
    if rep.rules:
        r = rep.rules
        lines += ["", "## Правила (шаг 1, без LLM)", "",
                  f"- кейсов: {r['n']} ({r.get('counts')})",
                  f"- behavior accuracy: {r['behavior_accuracy']}",
                  f"- FP на negative: {r['fp_rate_negative']}",
                  f"- trigger coverage: {r.get('trigger_coverage')} (не гейтится)",
                  f"- target coverage: {r.get('target_coverage')} (не гейтится)",
                  f"- ошиблись: {', '.join(r.get('misclassified') or []) or '—'}"]
    if rep.agent:
        a = rep.agent
        lines += ["", "## Агент (шаг 2)", "",
                  f"- backend: `{a['backend']}`, write-кейсов: {a['n_write_docs_cases']}",
                  f"- drafts_produced_rate: {a['drafts_produced_rate']}",
                  f"- target_match_rate: {a['target_match_rate']}",
                  f"- must_include_hit_rate: {a['must_include_hit_rate']}",
                  f"- hallucination_free_rate: {a['hallucination_free_rate']}",
                  f"- dropped_total: {a['dropped_total']}"]
        bad = [r["id"] for r in a["rows"] if not r["target_match"]]
        if bad:
            lines.append(f"- target mismatch: {', '.join(bad)}")
    if rep.errors:
        lines += ["", "## Ошибки прогона", ""] + [f"- {e}" for e in rep.errors]
    return "\n".join(lines) + "\n"


def save(rep: EvalReport, out_dir: Path | None = None) -> tuple[Path, Path]:
    out = Path(out_dir or (REPO / "data" / "reports"))
    out.mkdir(parents=True, exist_ok=True)
    js = out / "eval_gold.json"
    md = out / "eval_gold.md"
    js.write_text(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_markdown(rep), encoding="utf-8")
    return js, md
