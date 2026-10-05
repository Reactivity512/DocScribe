"""LLM-обёртка для verify_api_change (OpenAI-совместимый API: Ollama / vLLM).

Ключевое свойство: сервер остаётся работоспособным без LLM. Если бэкенд недоступен,
модель не отвечает или выдаёт мусор — возвращается детерминированный rule-fallback
с пометкой used_llm=false. Для MVP это важнее, чем «умный» ответ.
"""

from __future__ import annotations

import json
import re

from docagent.config import Settings
from docagent.mcp_server.models import ApiChange, VerificationVerdict, VerifyResult

_SYSTEM_RU = (
    "Ты — ревизор изменений публичного Python API. Тебе дают дифф символа. "
    "Определи, затронул ли change контракт, видимый пользователю библиотеки "
    "(имя, параметры, типы, значения по умолчанию, возвращаемое значение, семантика). "
    "Отвечай строго JSON: {\"verdict\": \"user_visible|internal_only|uncertain\", "
    "\"confidence\": 0..1, \"reasoning\": \"одно предложение\"}."
)


def build_prompt(change: ApiChange, lang: str = "ru") -> str:
    lines = [
        f"Символ: {change.symbol}",
        f"Файл: {change.file}",
        f"Тип изменения: {change.kind.value}",
    ]
    if change.old_signature:
        lines.append(f"Было: {change.old_signature}")
    if change.new_signature:
        lines.append(f"Стало: {change.new_signature}")
    if change.param_diff:
        lines.append("Дельта параметров: " + json.dumps(change.param_diff, ensure_ascii=False))
    if change.reasons:
        lines.append(("Причины (детерминированные): " if lang == "ru" else "Deterministic reasons: ")
                     + "; ".join(change.reasons))
    return "\n".join(lines)


def _rule_fallback(change: ApiChange) -> VerifyResult:
    """Детерминированный вердикт — используется и как fallback, и как baseline."""
    breaking = bool(change.breaking)
    pd = change.param_diff or {}
    visible = (
        change.kind.value in ("added", "removed")
        or breaking
        or bool(pd.get("annotation_changed"))
        or bool(pd.get("default_changed"))
    )
    if change.kind.value == "changed" and not visible:
        verdict = VerificationVerdict.uncertain
        conf, why = 0.5, "только переупорядочивание/косметика сигнатуры — нужен человек"
    elif visible:
        verdict = VerificationVerdict.user_visible
        conf = 0.9 if breaking or change.kind.value != "changed" else 0.75
        why = "изменение контракта видно из сигнатуры"
    else:
        verdict = VerificationVerdict.internal_only
        conf, why = 0.8, "публичная сигнатура не изменилась"
    return VerifyResult(
        verdict=verdict,
        user_visible=visible,
        confidence=conf,
        reasoning=why,
        backend="rule_fallback",
        used_llm=False,
    )


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _parse_verdict(text: str) -> tuple[VerificationVerdict | None, float, str]:
    m = _JSON_RE.search(text or "")
    if not m:
        low = (text or "").lower()
        for v in VerificationVerdict:
            if v.value.replace("_", " ") in low or v.value in low:
                return v, 0.4, text.strip()[:200]
        return None, 0.0, ""
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, 0.0, ""
    raw = str(obj.get("verdict", "")).strip().lower()
    mapping = {
        "user_visible": VerificationVerdict.user_visible,
        "internal_only": VerificationVerdict.internal_only,
        "uncertain": VerificationVerdict.uncertain,
    }
    verdict = mapping.get(raw)
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return verdict, max(0.0, min(1.0, conf)), str(obj.get("reasoning", ""))[:400]


class LLMClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = None

    def available(self) -> bool:
        return self.s.use_llm

    def _get(self):
        if self._client is None:
            from openai import OpenAI  # ленивый импорт

            self._client = OpenAI(
                base_url=self.s.openai_base_url,
                api_key=self.s.api_key,
                timeout=self.s.llm_timeout_s,
            )
        return self._client

    def complete(self, system: str, user: str) -> str:
        resp = self._get().chat.completions.create(
            model=self.s.model,
            temperature=self.s.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()


def verify_change(change: ApiChange, settings: Settings, client: LLMClient | None = None) -> VerifyResult:
    """LLM-верификация с гарантированным fallback на правила."""
    fb = _rule_fallback(change)
    if not settings.use_llm:
        return fb
    client = client or LLMClient(settings)
    try:
        text = client.complete(_SYSTEM_RU, build_prompt(change, settings.docs_lang))
    except Exception as e:  # сеть/таймаут/5xx — деградируем молча
        fb.reasoning = f"{fb.reasoning}; LLM недоступен ({type(e).__name__}: {str(e)[:120]})"
        fb.backend = "rule_fallback_after_error"
        return fb
    verdict, conf, why = _parse_verdict(text)
    if verdict is None:
        fb.reasoning = f"{fb.reasoning}; LLM ответил неформатно: {text[:120]!r}"
        fb.backend = "rule_fallback_bad_json"
        return fb
    # согласование с детерминированным правилом: при конфликте понижаем уверенность
    agree = verdict.value == fb.verdict.value
    final_conf = conf if agree else min(conf, fb.confidence) * 0.8
    if not agree and fb.verdict is VerificationVerdict.uncertain:
        final_conf = max(conf, 0.5)
    return VerifyResult(
        verdict=verdict,
        user_visible=verdict is VerificationVerdict.user_visible,
        confidence=round(final_conf, 2),
        reasoning=why or fb.reasoning,
        backend=settings.docs_backend,
        model=settings.model,
        used_llm=True,
    )
