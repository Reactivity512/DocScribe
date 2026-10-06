"""Тесты шага 6: gold-гейт и онлайн-метрики приёмки (без сети)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from docagent.eval import gold as G  # noqa: E402
from docagent.eval import online as O  # noqa: E402

WORK = REPO / "data" / "work"


# --------------------------------------------------------------------- gold-гейт --
def test_rules_harness_matches_step1_metrics():
    """Гейт обязан читать тот же харнес шага 1, а не свою копию правил.

    Число — фактический baseline после починки gold-сетов и бага `str.strip`:
    он вырос с 0.8704 до 0.8889 (кейс gold-014 перестал теряться).
    """
    r = G.run_rules()
    assert r["n"] == 54
    assert r["behavior_accuracy"] == 0.8889
    assert r["fp_rate_negative"] == 0.0


def test_evaluate_reports_pass_and_fail():
    rep = G.evaluate(backend="fake", limit=3)
    names = {c["name"] for c in rep.checks}
    assert {"rules.behavior_accuracy", "rules.fp_rate_negative",
            "agent.drafts_produced_rate", "agent.target_match_rate"} <= names
    # limit=3 режет и правила, и агента: проверяем, что обе части посчитаны
    assert rep.rules["n"] == 3
    assert rep.agent["n_write_docs_cases"] == 3
    assert rep.passed

    # строгий гейт на полном сете правил (0.8889 < 1.0) обязан покраснеть
    strict = G.Gates(rules_behavior_accuracy=1.0)
    rep2 = G.evaluate(backend="fake", rules_limit=None, gates=strict, limit=2)
    assert not rep2.passed
    failed = [c for c in rep2.checks if not c["ok"]]
    assert failed and failed[0]["name"] == "rules.behavior_accuracy"
    assert rep2.rules["n"] == 54


def test_markdown_report_has_verdict_and_thresholds():
    rep = G.evaluate(backend="fake", limit=2)
    md = G.render_markdown(rep)
    assert md.startswith("# Eval")
    assert ("PASS" in md) or ("FAIL" in md)
    assert "| Проверка |" in md
    assert "порог" in md


def test_save_writes_json_and_md():
    out = WORK / "eval-test-out"
    out.mkdir(parents=True, exist_ok=True)
    rep = G.evaluate(backend="fake", limit=2)
    js, md = G.save(rep, out)
    try:
        data = json.loads(Path(js).read_text(encoding="utf-8"))
        assert data["passed"] is True and data["checks"]
        assert Path(md).read_text(encoding="utf-8").startswith("# Eval")
    finally:
        for p in (js, md):
            Path(p).unlink(missing_ok=True)
        try:
            out.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------- онлайн-метрики --
def _make_ckpt_db(path: Path) -> None:
    """Синтетический чекпоинт: два треда — merged с 1 правкой и опубликованный без ревью."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    cfg1 = {"configurable": {"thread_id": "pr-merged", "checkpoint_ns": ""}}
    saver.put(cfg1, {"v": 1, "id": "c1", "ts": "2026-10-07T10:00:00Z",
                     "channel_values": {"status": ["revised_1", "published"],
                                        "pr_number": 7, "pr_url": "u7",
                                        "publish_target": "github",
                                        "revisions_count": 1,
                                        "behavior": "write_docs"},
                     "channel_versions": {}, "versions_seen": {}}, {}, {})
    cfg2 = {"configurable": {"thread_id": "pr-open", "checkpoint_ns": ""}}
    saver.put(cfg2, {"v": 1, "id": "c2", "ts": "2026-10-07T11:00:00Z",
                     "channel_values": {"status": ["drafts_ok", "published"],
                                        "pr_number": 8, "pr_url": "u8",
                                        "publish_target": "github",
                                        "revisions_count": 0,
                                        "behavior": "write_docs"},
                     "channel_versions": {}, "versions_seen": {}}, {}, {})
    cfg3 = {"configurable": {"thread_id": "pr-silent", "checkpoint_ns": ""}}
    saver.put(cfg3, {"v": 1, "id": "c3", "ts": "2026-10-07T12:00:00Z",
                     "channel_values": {"status": ["silent:stay_silent"],
                                        "behavior": "stay_silent"},
                     "channel_versions": {}, "versions_seen": {}}, {}, {})
    conn.commit()
    conn.close()


def test_online_metrics_aggregate_checkpoints_and_watch_state():
    ckpt = WORK / "eval-ckpt.db"
    workdir = WORK / "eval-watch"
    workdir.mkdir(parents=True, exist_ok=True)
    _make_ckpt_db(ckpt)
    (workdir / "pr-merged.watch.json").write_text(json.dumps({
        "thread_id": "pr-merged", "pr_number": 7, "done": "merged",
        "processed": [101, 102]}), encoding="utf-8")
    (workdir / "pr-open.watch.json").write_text(json.dumps({
        "thread_id": "pr-open", "pr_number": 8, "processed": []}), encoding="utf-8")

    m = O.collect(ckpt=ckpt, workdir=workdir, outbox=WORK / "no-outbox")
    t = m.totals
    assert t["runs"] == 3
    assert t["published"] == 2
    assert t["silent_skipped"] == 1
    assert t["merged_by_lead"] == 1
    assert t["awaiting_review"] == 1
    assert t["merged_rate"] == 0.5
    assert t["revisions_to_merge_avg"] == 1.0
    assert m.by_intent == {"merged": 1, "awaiting_review": 1}

    md = O.render_markdown(m)
    assert "merged_rate" in md and "| `runs` | 3 |" in md

    ckpt.unlink(missing_ok=True)
    # sqlite оставляет рядом -wal/-shm: без чистки они копятся в рабочем каталоге
    for suffix in ("-wal", "-shm"):
        Path(str(ckpt) + suffix).unlink(missing_ok=True)
    for p in workdir.glob("*.watch.json"):
        p.unlink(missing_ok=True)
    try:
        workdir.rmdir()
    except OSError:
        pass


def test_online_metrics_empty_when_nothing_ran():
    m = O.collect(ckpt=WORK / "no-such.db", workdir=WORK / "no-such-dir",
                  outbox=WORK / "no-outbox")
    assert m.totals["runs"] == 0
    assert m.totals["merged_rate"] is None
