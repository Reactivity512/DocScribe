"""LLM-агент документации (шаг 2).

Лёгкий собственный ReAct-loop без LangChain: оркестрацию графа делает LangGraph
на шаге 3, здесь — только цикл "контекст -> LLM -> валидированный JSON-черновик".

Инструменты вызываются как прямые Python-функции сервисного слоя
(docagent.mcp_server.service), тот же код обслуживает MCP-транспорт.
"""

from docagent.agent.backends import FakeBackend, OpenAICompatBackend, get_backend
from docagent.agent.loop import AgentResult, DocsAgent, ToolProvider
from docagent.agent.schemas import DocDraft, DraftAction, DraftList

__all__ = [
    "DocDraft",
    "DraftAction",
    "DraftList",
    "FakeBackend",
    "OpenAICompatBackend",
    "get_backend",
    "AgentResult",
    "DocsAgent",
    "ToolProvider",
]
