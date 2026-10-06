"""Сборка графа LangGraph + выбор чекпоинтера (Шаг 3).

MVP: SqliteSaver (файл data/checkpoints.db). Прод: если DOCAGENT_CHECKPOINT_URI
начинается с postgres:// — используем langgraph-checkpoint-postgres.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from . import nodes as N
from .state import OrchState


def make_checkpointer(s=None) -> tuple[BaseCheckpointSaver, object | None]:
    """Возвращает (saver, ctx_manager_или_None). Для Sqlite держим соединение открытым."""
    s = s or get_settings()
    uri = (s.checkpoint_uri or "").strip()
    if uri.startswith("postgres"):
        # прод-путь; пакет опциональный, чтобы MVP не тянул psycopg
        from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore
        saver = PostgresSaver.from_conn_string(uri)
        saver.setup()
        return saver, None
    from langgraph.checkpoint.sqlite import SqliteSaver
    db = Path(s.repo_root) / "data" / "checkpoints.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    return SqliteSaver(conn), conn


def build_graph(checkpointer: BaseCheckpointSaver):
    g = StateGraph(OrchState)

    g.add_node("fetch_pr", N.fetch_pr)
    g.add_node("analyze_diff", N.analyze_diff)
    g.add_node("generate_drafts", N.generate_drafts)
    g.add_node("self_check", N.self_check)
    g.add_node("prepare_payload", N.prepare_payload)
    g.add_node("human_approval", N.human_approval)
    g.add_node("revise", N.revise)
    g.add_node("escalate_manual", N.escalate_manual)
    g.add_node("publish", N.publish)
    g.add_node("publish_first", N.publish_first)
    g.add_node("finalize_silent", N.finalize_silent)
    g.add_node("finalize_rejected", N.finalize_rejected)
    g.add_node("finalize_published", N.finalize_published)

    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze_diff")
    g.add_conditional_edges("analyze_diff", N.route_after_analyze,
                            {"generate_drafts": "generate_drafts",
                             "finalize_silent": "finalize_silent"})
    g.add_conditional_edges("generate_drafts", N.route_after_generate,
                            {"self_check": "self_check",
                             "human_approval": "human_approval"})
    g.add_edge("self_check", "prepare_payload")
    # порядок шага 5: для github PR создаётся до ожидания ревью (publish_first ->
    # HITL), для outbox interrupt остаётся до записи (prepare_payload -> human_approval).
    # Публикация разведена на две ноды намеренно: цикл publish -> human_approval
    # в LangGraph терял признаки, выставленные публикацией, и граф зацикливался.
    g.add_conditional_edges("prepare_payload", N.route_after_payload,
                            {"publish_first": "publish_first",
                             "human_approval": "human_approval"})
    g.add_conditional_edges("publish_first", N.route_after_first_publish,
                            {"human_approval": "human_approval",
                             "finalize_published": "finalize_published"})
    g.add_conditional_edges("human_approval", N.route_after_approval,
                            {"publish": "publish",
                             "revise": "revise",
                             "escalate_manual": "escalate_manual",
                             "finalize_rejected": "finalize_rejected"})
    g.add_edge("revise", "self_check")          # цикл правок через те же проверки
    g.add_edge("escalate_manual", "publish")    # лимит исчерпан -> публикуем с пометкой
    g.add_edge("finalize_silent", END)
    g.add_edge("finalize_rejected", END)
    g.add_edge("finalize_published", END)

    return g.compile(checkpointer=checkpointer)


def compile_pipeline():
    """Удобство для тестов/CLI: (graph, cleanup_callable)."""
    saver, conn = make_checkpointer()
    graph = build_graph(saver)

    def cleanup():
        if conn is not None:
            conn.close()
    return graph, cleanup
