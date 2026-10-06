"""CLI оркестратора (Шаг 3).

Примеры:
  # один gold-кейс насквозь, fake-бэкенд, автоаппрув для демо:
  python -m docagent.orchestrator.cli --gold gold-005 --backend fake --auto-approve

  # интерактивный HITL в терминале (эмуляция решения лида; шаг 5 заменит на GitHub):
  python -m docagent.orchestrator.cli --diff path/to/file.diff --backend ollama

  # продолжить прерванный thread после kill процесса:
  python -m docagent.orchestrator.cli --resume --thread pr-gold-005 --decision approve
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # src/docagent/orchestrator/cli.py -> корень репо
sys.path.insert(0, str(REPO / "src"))

from langgraph.types import Command  # noqa: E402

from docagent.orchestrator.graph import compile_pipeline  # noqa: E402


def _load_gold_diff(case_id: str) -> str:
    p = REPO / "data" / "gold" / "cases" / f"{case_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))["code_change"]["diff"]


def _ask_human(payload_view: dict) -> dict:
    """Консольный HITL. Шаг 5: этот ввод придёт из GitHub-комментариев/webhook."""
    print("\n=== ОЖИДАЕТСЯ РЕШЕНИЕ TEAM LEAD ===")
    print(json.dumps(payload_view, ensure_ascii=False, indent=2))
    dec = input("decision [approve/reject/changes]: ").strip().lower()
    fb = ""
    if dec == "changes":
        fb = input("замечания для переписывания: ").strip()
    return {"decision": dec or "reject", "feedback": fb, "reviewer": "cli"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="DocAgent orchestrator (LangGraph)")
    ap.add_argument("--gold", help="id gold-кейса, напр. gold-005")
    ap.add_argument("--diff", help="путь к .diff файлу")
    ap.add_argument("--backend", default="fake", choices=["fake", "ollama", "vllm"])
    ap.add_argument("--lang", default=None, choices=["ru", "en"])
    ap.add_argument("--auto-approve", action="store_true",
                    help="автоматически approve при interrupt (для e2e-демо/тестов)")
    ap.add_argument("--once", action="store_true",
                    help="не чистить thread: прогнать кейс один раз (иначе --gold "
                         "кейсы переиспользуют thread_id и статус-чейны склеиваются)")
    ap.add_argument("--resume", action="store_true", help="продолжить существующий thread")
    ap.add_argument("--thread", help="thread_id для --resume")
    ap.add_argument("--pr-ref", default="",
                    help="ссылка на реальный PR (url или owner/repo#N) — нужна ноде "
                         "publish при DOCAGENT_PUBLISH_TARGET=github; без неё публикация "
                         "ушла бы в base-репозиторий и GitHub вернул бы "
                         "'No commits between main and <branch>'")
    ap.add_argument("--decision", default="approve",
                    choices=["approve", "reject", "changes"])
    ap.add_argument("--feedback", default="")
    args = ap.parse_args(argv)

    graph, cleanup = compile_pipeline()
    cfg = {"configurable": {"thread_id": args.thread or "run-1"}}

    if not args.resume and not args.once:
        # новый запуск того же кейса = новый тред (иначе статус-чейны накапливаются
        # поверх старого чекпоинта pr-<id>)
        cfg["configurable"]["thread_id"] += f"-{int(time.time())}"

    if args.resume:
        if not args.thread:
            ap.error("--resume требует --thread")
        decision = {"decision": args.decision, "feedback": args.feedback,
                    "reviewer": "cli"}
        result = graph.invoke(Command(resume=decision), cfg)
    else:
        if not (args.gold or args.diff):
            ap.error("нужно --gold <id> или --diff <path>")
        diff = _load_gold_diff(args.gold) if args.gold else \
            Path(args.diff).read_text(encoding="utf-8")
        state_in = {
            "pr_ref": args.pr_ref or args.gold or args.diff,
            "diff_text": diff,
            "backend_name": args.backend,
            "lang": args.lang or "",
            "thread_id": args.thread or f"pr-{args.gold or Path(args.diff).stem}",
        }
        cfg["configurable"]["thread_id"] = state_in["thread_id"]
        result = graph.invoke(state_in, cfg)
        # дослушать interrupts, пока граф не дойдёт до END
        while isinstance(result, dict) and "__interrupt__" in result:
            intr = result["__interrupt__"][0].value
            if args.auto_approve:
                decision = {"decision": "approve", "feedback": "", "reviewer": "auto"}
            else:
                decision = _ask_human(intr)
            result = graph.invoke(Command(resume=decision), cfg)

    cleanup()
    final = result if isinstance(result, dict) else {}
    out = {
        "thread_id": cfg["configurable"]["thread_id"],
        "status_chain": final.get("status", []),
        "drafts": len(final.get("drafts", [])),
        "drop_reasons": final.get("drop_reasons", []),
        "revisions": final.get("revisions_count", 0),
        "outbox_path": final.get("outbox_path", ""),
        "events": final.get("events", []),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
