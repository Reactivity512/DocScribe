"""Онлайн-метрики приёмки (шаг 6): что реально произошло с PR в демо-репозитории.

Источник — локальные артефакты, а не GitHub (сеть не нужна, работает офлайн):
  * data/checkpoints.db — потоки графа: статусы прогона (silent/published/…);
  * data/work/<thread>.watch.json — что делал watcher (комментарии, правки, мерж);
  * data/outbox/*.json — публикации в режиме outbox.

Метрики отвечают на вопрос заказчика «сколько правок до принятия»:
  * merged_rate — доля опубликованных PR, которые лид смержил;
  * revisions_to_merge — сколько правок понадобилось до мержа (0 = приняли сразу);
  * awaiting_review — PR, которые висят и ждут решения (наш незакрытый хвост);
  * intent-разбивка комментариев: сколько было «поправь» против «почему».
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SILENT = "silent"
PUBLISHED = "published"
NEEDS_EDIT = "needs_human_edit"
REJECTED = "rejected"


@dataclass
class OnlineMetrics:
    threads: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    by_intent: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"totals": self.totals, "by_intent": self.by_intent,
                "threads": self.threads}


def _checkpoint_values(ckpt: Path) -> dict[str, dict]:
    """thread_id -> значения состояния из checkpoint-базы (самый поздний чекпоинт).

    Чекпоинты LangGraph пишутся как msgpack, поэтому разбираем их штатным
    сериализатором (JsonPlusSerializer), а не json.loads: иначе состояние не
    прочитать, и метрики молча показывают ноль прогонов.
    """
    if not ckpt.exists():
        return {}
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        conn = sqlite3.connect(f"file:{ckpt}?mode=ro", uri=True)
        rows = conn.execute(
            "select thread_id, checkpoint_id, type, checkpoint from checkpoints"
        ).fetchall()
        conn.close()
    except (sqlite3.Error, ImportError):
        return {}
    serde = JsonPlusSerializer()
    best: dict[str, tuple[str, dict]] = {}
    for tid, cid, ctype, blob in rows:
        try:
            payload = serde.loads_typed((ctype, blob))
        except Exception:  # noqa: BLE001 — старые/битые чекпоинты пропускаем
            continue
        values = _extract_values(payload)
        if not values:
            continue
        prev = best.get(tid)
        if prev is None or str(cid) >= prev[0]:
            best[tid] = (str(cid), values)
    return {tid: values for tid, (_cid, values) in best.items()}


def _extract_values(payload: dict) -> dict:
    """Достаёт канал 'channel_values' из сериализованного чекпоинта."""
    if not isinstance(payload, dict):
        return {}
    for key in ("channel_values", "values"):
        v = payload.get(key)
        if isinstance(v, dict):
            return v
    # msgspec/msgpack-подобные структуры: ищем вложенный dict с thread_id
    for v in payload.values():
        if isinstance(v, dict) and ("status" in v or "thread_id" in v):
            return v
    return {}


def _watch_states(workdir: Path) -> list[dict]:
    if not workdir.exists():
        return []
    out = []
    for p in sorted(workdir.glob("*.watch.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d["_file"] = p.name
        out.append(d)
    return out


def _intent_of_state(d: dict) -> str:
    if d.get("done") == "merged":
        return "merged"
    if d.get("done") == "closed":
        return "closed"
    return "awaiting_review"


def collect(ckpt: Path | None = None, workdir: Path | None = None,
            outbox: Path | None = None) -> OnlineMetrics:
    ckpt = Path(ckpt or (REPO / "data" / "checkpoints.db"))
    workdir = Path(workdir or (REPO / "data" / "work"))
    outbox = Path(outbox or (REPO / "data" / "outbox"))

    values = _checkpoint_values(ckpt)
    watches = {d.get("thread_id") or d["_file"].replace(".watch.json", ""): d
               for d in _watch_states(workdir)}
    outbox_files = {p.stem for p in outbox.glob("*.json")} if outbox.exists() else set()

    m = OnlineMetrics()
    intents: Counter = Counter()
    published = merged = awaiting = silent = 0
    revisions_to_merge: list[int] = []
    for tid in sorted(set(values) | set(watches)):
        st = values.get(tid, {})
        w = watches.get(tid, {})
        status_list = st.get("status") or []
        if isinstance(status_list, str):
            status_list = [status_list]
        status_set = set(status_list)
        action_counts = Counter()
        if w:
            for p in w.get("processed", []):
                # в состоянии watcher'а хранятся только id; интент — в actions прогона
                action_counts["processed"] += 1
            intents[_intent_of_state(w)] += 1
        is_published = bool(status_set & {PUBLISHED, NEEDS_EDIT}) or tid in outbox_files
        is_silent = any(str(s).startswith(SILENT) for s in status_list)
        revisions = int(st.get("revisions_count") or 0)
        if is_published:
            published += 1
            if w.get("done") == "merged":
                merged += 1
                revisions_to_merge.append(revisions)
            elif w.get("done") != "closed":
                awaiting += 1
        elif is_silent:
            silent += 1
        m.threads.append({
            "thread_id": tid,
            "status": status_list[-3:],
            "behavior": st.get("behavior", ""),
            "pr": st.get("pr_number") or w.get("pr_number"),
            "pr_url": st.get("pr_url") or w.get("pr_url", ""),
            "publish_target": st.get("publish_target", ""),
            "revisions": revisions,
            "comments_seen": action_counts.get("processed", 0),
            "outcome": _intent_of_state(w) if w else ("silent" if is_silent else "no_watch"),
        })

    total_runs = len(m.threads)
    m.totals = {
        "runs": total_runs,
        "published": published,
        "silent_skipped": silent,
        "merged_by_lead": merged,
        "awaiting_review": awaiting,
        "merged_rate": round(merged / published, 4) if published else None,
        "revisions_to_merge_avg": round(sum(revisions_to_merge) / len(revisions_to_merge), 2)
        if revisions_to_merge else None,
        "revisions_to_merge_max": max(revisions_to_merge) if revisions_to_merge else None,
        "merged_without_revision": sum(1 for r in revisions_to_merge if r == 0),
    }
    m.by_intent = dict(intents)
    return m


def render_markdown(m: OnlineMetrics) -> str:
    t = m.totals
    lines = ["# Eval — онлайн-метрики приёмки (шаг 6)", "",
             "Источник: локальные артефакты (`data/checkpoints.db`, "
             "`data/work/*.watch.json`, `data/outbox/`).", "",
             "| Метрика | Значение |", "|---|---|"]
    for key in ("runs", "published", "silent_skipped", "merged_by_lead",
                "awaiting_review", "merged_rate", "revisions_to_merge_avg",
                "revisions_to_merge_max", "merged_without_revision"):
        lines.append(f"| `{key}` | {t.get(key)} |")
    if m.by_intent:
        lines += ["", "## Исходы тредов", ""]
        for k, v in sorted(m.by_intent.items()):
            lines.append(f"- {k}: {v}")
    rows = [x for x in m.threads if x["pr"] or x["revisions"]]
    if rows:
        lines += ["", "## Треды", "",
                  "| thread | PR | статус | ревизий | исход |", "|---|---|---|---|---|"]
        for x in rows:
            lines.append(f"| `{x['thread_id']}` | {x['pr']} | "
                         f"{'/'.join(map(str, x['status']))} | {x['revisions']} | "
                         f"{x['outcome']} |")
    return "\n".join(lines) + "\n"


def save(m: OnlineMetrics, out_dir: Path | None = None) -> tuple[Path, Path]:
    out = Path(out_dir or (REPO / "data" / "reports"))
    out.mkdir(parents=True, exist_ok=True)
    js = out / "eval_online.json"
    md = out / "eval_online.md"
    js.write_text(json.dumps(m.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_markdown(m), encoding="utf-8")
    return js, md
