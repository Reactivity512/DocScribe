"""Watcher HITL-цикла (шаг 5): комментарии PR -> правка/пояснение в том же PR.

Роль: связать GitHub и граф. Граф останавливается в `human_approval` (interrupt)
и ждёт решение; watcher опрашивает комментарии PR и вызывает
`graph.invoke(Command(resume=...))` — по одному разу на комментарий.

Модель заказчика (зафиксирована в docs/plan/STEP4-6_github_hitl_eval.md):
  * бот НЕ мержит: PR обычный, решение «готово» выражается мержем лида;
  * любой комментарий человека получает реакцию: правку или объяснение;
  * правка = новый коммит в тот же PR, отдельный PR не создаётся;
  * после мержа тред закрывается.

Идемпотентность: обработанные id комментариев хранятся в файле состояния
(data/work/<thread>.watch.json) — перезапуск процесса не приводит к повторной
обработке и повторному resume.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from .refs import pr_slug, repo_and_pr
from .review import CHANGES, classify_comment, reply_for

WATCH_STATE_SUFFIX = ".watch.json"


# ------------------------------------------------------------------- состояние --
def state_path(s: Settings, thread_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in thread_id)
    return Path(s.workdir) / f"{safe}{WATCH_STATE_SUFFIX}"


def load_state(s: Settings, thread_id: str) -> dict:
    p = state_path(s, thread_id)
    if not p.exists():
        return {"thread_id": thread_id, "processed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"thread_id": thread_id, "processed": []}
    data.setdefault("processed", [])
    data.setdefault("thread_id", thread_id)
    return data


def save_state(s: Settings, thread_id: str, data: dict) -> None:
    p = state_path(s, thread_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- обработка --
@dataclass
class WatchResult:
    thread_id: str
    actions: list[dict] = field(default_factory=list)
    done: bool = False
    reason: str = ""

    @property
    def changed(self) -> bool:
        return any(a["action"] == "revised" for a in self.actions)


def _resume(graph, tid: str, decision: dict):
    from langgraph.types import Command
    return graph.invoke(Command(resume=decision), {"configurable": {"thread_id": tid}})


def process_once(
    *,
    thread_id: str,
    branch: str,
    graph,
    client,
    s: Settings | None = None,
    pr_ref: str = "",
    pr_number: int | None = None,
    bot_login: str = "",
    dry_run: bool = False,
) -> WatchResult:
    """Один проход: снять новые комментарии и отреагировать на каждый.

    Порядок действий важен: сначала ответ в тред (лид сразу видит реакцию), потом
    правка и отдельное подтверждение — даже если генерация упадёт, комментарий
    не останется без ответа.
    """
    s = s or get_settings()
    res = WatchResult(thread_id=thread_id)
    wstate = load_state(s, thread_id)
    processed: set = set(wstate.get("processed", []))

    slug = pr_slug({"pr_ref": pr_ref, "pr_slug": ""}, s) if pr_ref else s.demo_repo
    number = pr_number or repo_and_pr(pr_ref)[1]
    if not number:
        found = client.find_pr(slug, branch)
        if not found:
            res.reason = "PR не найден"
            return res
        number = found["number"]
    pr = client.find_pr(slug, branch) or {}
    wstate.update({"pr_slug": slug, "pr_number": number,
                   "pr_url": pr.get("url", f"https://github.com/{slug}/pull/{number}")})

    if pr.get("merged"):
        res.done, res.reason = True, "PR смержен лидом"
        res.actions.append({"action": "merged", "pr": number})
        wstate["done"] = "merged"
        if not dry_run:
            save_state(s, thread_id, wstate)
        return res
    if pr.get("state") == "closed":
        res.done, res.reason = True, "PR закрыт без мержа"
        res.actions.append({"action": "closed", "pr": number})
        wstate["done"] = "closed"
        if not dry_run:
            save_state(s, thread_id, wstate)
        return res

    bot = bot_login or client.bot_login()
    state = graph.get_state({"configurable": {"thread_id": thread_id}}).values or {}
    if not state:
        res.reason = "нет состояния графа для thread (граф не запускался?)"
        return res

    for c in client.list_comments(slug, number):
        if c["id"] in processed:
            continue
        if bot and c.get("author") == bot:
            processed.add(c["id"])          # свои комментарии — не повод для цикла
            continue
        intent = classify_comment(c.get("body", ""))
        res.actions.append({"action": "classified", "comment": c["id"],
                            "kind": intent.kind, "reason": intent.reason,
                            "author": c.get("author")})
        if dry_run:
            processed.add(c["id"])
            continue

        reply = reply_for(intent, state, c)
        client.comment(slug, number, reply)
        res.actions.append({"action": "replied", "comment": c["id"]})

        if intent.needs_rewrite:
            out = _resume(graph, thread_id, {"decision": CHANGES,
                                             "feedback": c.get("body", ""),
                                             "reviewer": c.get("author", "")})
            state = out if isinstance(out, dict) else state
            if state.get("updated"):    # publish вернул updated=True => PR тот же
                client.comment(slug, number,
                               "Черновик обновлён и запушен в этот же PR — "
                               "посмотрите, пожалуйста, актуальный diff.")
                res.actions.append({"action": "revised", "comment": c["id"]})
            else:
                client.comment(slug, number,
                               "Не удалось обновить черновик автоматически — "
                               "нужна ручная правка.")
                res.actions.append({"action": "revise_failed", "comment": c["id"]})
        processed.add(c["id"])
        wstate["processed"] = sorted(processed)
        save_state(s, thread_id, wstate)

    wstate["processed"] = sorted(processed)
    if not dry_run:
        save_state(s, thread_id, wstate)
    return res


def watch(
    *,
    thread_id: str,
    branch: str,
    graph,
    client,
    s: Settings | None = None,
    pr_ref: str = "",
    interval_s: float = 15.0,
    once: bool = False,
    dry_run: bool = False,
    max_rounds: int = 0,
) -> WatchResult:
    """Polling-цикл: опрашивает комментарии, пока PR не смержен/закрыт.

    Вебхук для демо-репозитория не нужен (решение шага 5): тот же process_once
    потом вызовется из вебхук-обработчика без изменений.
    """
    s = s or get_settings()
    rounds = 0
    while True:
        rounds += 1
        res = process_once(thread_id=thread_id, branch=branch, graph=graph,
                           client=client, s=s, pr_ref=pr_ref, dry_run=dry_run)
        if res.done or once or (max_rounds and rounds >= max_rounds):
            return res
        time.sleep(max(interval_s, 1.0))


def env_interval(default: float = 15.0) -> float:
    try:
        return float(os.environ.get("DOCAGENT_WATCH_INTERVAL_S", default))
    except ValueError:
        return default
