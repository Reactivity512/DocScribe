"""DocsAgent: лёгкий собственный ReAct-loop (шаг 2, вариант A без LangChain).

Цикл на один PR:
  1. deterministic decision через сервисный слой (он же MCP-инструмент should_update_docs);
  2. если stay_silent — выход (агент не болтает);
  3. собрать контекст инструментами (api_snapshot, read_file существующей секции);
  4. для каждого expected target — ОДИН вызов LLM → JSON → guard-проверки;
     при провале retry с текстом ошибки (до max_retries), затем drop;
  5. вернуть AgentResult со списком DocDraft (из него шаг 4 соберёт draft PR).

Инструменты — прямые вызовы service-слоя; на шаге 3 тот же цикл будет запускаться
графом, а при подключении внешних MCP-серверов заменим только ToolProvider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from docagent.agent import prompts
from docagent.agent.backends import get_backend
from docagent.agent.guards import validate_draft_full
from docagent.agent.schemas import DocDraft, parse_llm_json, validate_draft
from docagent.config import Settings, get_settings
from docagent.mcp_server import service
from docagent.mcp_server.models import DocsDecision, ExpectedDocsTarget


@dataclass
class AgentTraceStep:
    tool: str
    detail: str
    ms: int = 0


@dataclass
class AgentResult:
    decision: DocsDecision
    drafts: list[DocDraft] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)   # {target, error, attempts}
    trace: list[AgentTraceStep] = field(default_factory=list)
    llm_calls: int = 0
    backend_name: str = ""

    @property
    def produced(self) -> bool:
        return bool(self.drafts)


class ToolProvider:
    """Обёртка над сервисным слоем: единая точка правды для агента и MCP-транспорта."""

    def __init__(self, settings: Settings, repo_path: str | None = None):
        self.s = settings
        self.repo_path = repo_path

    def decide(self, diff_text: str) -> DocsDecision:
        return service.decide_on_diff(diff_text, self.s)

    def allowed_symbols(self, diff_text: str) -> list[str]:
        """Allow-list имён: символы из HEAD-снапшота + имена из изменённых файлов диффа."""
        names: list[str] = []
        try:
            root = service._resolve_root(self.repo_path)
            snap = service.api_snapshot(root, None)
            names.extend(sym.name for sym in snap)
        except Exception:
            pass
        analysis, parse, recs = service.analyze_diff(diff_text, self.s)
        for cs in analysis.changed_symbols:
            names.append(cs.name.split(".")[-1])
        for r in recs:
            names.append(r.symbol.split(".")[-1])
        names.extend(analysis.dependencies_added)
        names.extend(analysis.env_vars_touched)
        # детерминированный порядок, без дублей
        seen, out = set(), []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def existing_section(self, target: ExpectedDocsTarget, heading: str | None) -> str | None:
        """Читает текущий файл документации (для блока CURRENT DOC SECTION)."""
        path = prompts.TARGET_FILE_HINT.get(target, "").replace("NNNN-slug", "0001-bootstrap").replace("<topic>", "guide")
        if not path or "<" in path:
            return None
        try:
            root = service._resolve_root(self.repo_path)
            fc = service.read_file_at(root, path, None, self.s)
        except Exception:
            return None
        if not fc.exists or not fc.content.strip():
            return None
        if heading:
            m = heading.lstrip("#").strip().lower()
            lines = fc.content.splitlines()
            start = next((i for i, l in enumerate(lines)
                          if l.startswith("#") and l.lstrip("#").strip().lower() == m), None)
            if start is not None:
                end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
                           len(lines))
                return "\n".join(lines[start:end])[:3000]
        return fc.content[:3000]


class DocsAgent:
    def __init__(self, settings: Settings | None = None, repo_path: str | None = None,
                 backend=None, max_retries: int = 2):
        self.s = settings or get_settings()
        self.tools = ToolProvider(self.s, repo_path)
        self.backend = backend or get_backend(self.s)
        self.max_retries = max_retries

    # ------------------------------------------------------------------ run --
    def run(self, diff_text: str, lang: str | None = None) -> AgentResult:
        lang = (lang or self.s.docs_lang).lower()
        res = AgentResult(decision=None, backend_name=getattr(self.backend, "name", "?"))

        t0 = time.monotonic()
        decision = self.tools.decide(diff_text)
        res.trace.append(AgentTraceStep("should_update_docs",
                                        f"behavior={decision.behavior.value}",
                                        int((time.monotonic() - t0) * 1000)))
        res.decision = decision

        if decision.behavior.value != "write_docs":
            return res  # stay_silent / escalate_bootstrap: черновиков нет

        allowed = set(self.tools.allowed_symbols(diff_text))
        allowed_list = sorted(allowed)

        for target in decision.expected_docs_targets:
            draft, err = self._draft_for_target(target, decision, diff_text, allowed, allowed_list, lang, res)
            if draft is not None:
                res.drafts.append(draft)
            elif err is not None:
                res.dropped.append({"target": target.value, "error": err})
        return res

    # ------------------------------------------------------------- internals --
    def _draft_for_target(self, target, decision, diff_text, allowed, allowed_list, lang,
                          res: AgentResult):
        system = prompts.build_system(target.value, lang)
        user = prompts.build_user_prompt(
            target=target,
            decision=decision,
            diff_excerpt=diff_text,
            allowed_symbols=allowed_list,
            existing_section=self.tools.existing_section(target, None),
            lang=lang,
        )
        last_err = "no attempt"
        for attempt in range(1, self.max_retries + 2):
            t0 = time.monotonic()
            try:
                raw = self.backend.complete(system, user if attempt == 1 else user + prompts.RETRY_SUFFIX.format(error=last_err))
            except Exception as e:
                last_err = f"backend error: {type(e).__name__}: {str(e)[:150]}"
                res.trace.append(AgentTraceStep("llm.complete", f"attempt {attempt}: {last_err}",
                                                int((time.monotonic() - t0) * 1000)))
                res.llm_calls += 1
                continue
            res.llm_calls += 1
            res.trace.append(AgentTraceStep("llm.complete", f"attempt {attempt}, target={target.value}",
                                            int((time.monotonic() - t0) * 1000)))
            obj = parse_llm_json(raw)
            if obj is None:
                last_err = "reply is not a JSON object"
                continue
            if set(obj.keys()) == {"skipped_reason"}:
                return None, None  # модель осознанно пропустила — это валидный исход
            try:
                draft = validate_draft(obj)
            except (ValidationError, ValueError) as e:
                last_err = f"schema validation failed: {str(e)[:200]}"
                continue
            errs = validate_draft_full(draft, allowed)
            if errs:
                last_err = "; ".join(errs)
                continue
            return draft, None
        return None, f"{last_err} (after {self.max_retries + 1} attempts)"
