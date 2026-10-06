"""Классификация комментариев ревью и формулировки ответов (шаг 5).

Модель заказчика: комментарий человека в PR — это либо «внеси правку», либо
«объясни/неверно», и ни один комментарий не должен остаться без реакции бота.
Классификация детерминированная (правила ru/en), потому что цена ошибки
асимметрична: лишняя переписка хуже, чем лишний вопрос в треде.

Почему «начало реплики», а не поиск подстроки: подстрочный поиск ловит «add»
внутри «address» и «fix» внутри «fixture», то есть переписывал бы черновик по
обычному обсуждению. Русские и английские просьбы в ревью — это императив в
начале («добавь…», «please add…») с необязательным вежливым префиксом.

Порядок разбора (важен для смешанных комментариев):
  1. явная просьба изменить   -> changes   (переписать черновик)
  2. возражение/несогласие    -> objection (сначала объяснить, потом править)
  3. вопрос                   -> question  (ответ в треде, без переписки)
  4. всё остальное            -> clarify   (спросить, что именно поправить)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHANGES = "changes"
OBJECTION = "objection"
QUESTION = "question"
CLARIFY = "clarify"

_POLITE = r"(?:(?:пожалуйста|please|плиз)[\s,]+)?"
# императивы/просьбы в начале клаузы: «поправь», «добавьте», «сделай».
# «обнови» намеренно отсутствует: эта основа совпадает с прошедшим временем
# («обновил address»), поэтому берём «обновите»/«обновить».
_STEM_RU = (
    "добав", "исправ", "поправ", "перепиш", "перепи", "удали", "убери", "замен",
    "перестав", "переформулир", "уточни", "дополни", "допиши", "встав",
    "обновит", "обновите", "сделай", "поменя",
)
_STEM_EN = (
    "change", "rewrite", "reword", "update", "replace", "remove", "delete",
    "fix", "add", "insert", "append", "extend", "expand", "clarify",
)
# Русские основы + окончания императива (ь/те/ьте/и/а) и английские основы + не
# более одной буквы (-s/-es). Так «поправьте» ловится, а «fixed»/«address» — нет.
_IMPERATIVE_RE = re.compile(
    rf"^{_POLITE}(?:(?:{'|'.join(_STEM_RU)})(?:ьте|ь|те|и|а|е)?|"
    rf"(?:{'|'.join(_STEM_EN)})[a-z]?)(?![a-zа-яё])", re.I)
# просьбы не в начале (частая русская формулировка «нужно/надо …»)
_CHANGE_PHRASE_RU = ("нужно", "надо", "давай", "требуется", "стоит")
_CHANGE_PHRASE_EN = ("should be", "needs to", "must be", "could you", "can you",
                     "please", "would be better")
_OBJECTION_RU = ("неверн", "неправильн", "некорректн", "ошиб", "не согласен",
                 "не согласна", "не так", "не то", "ерунд", "чушь", "нельзя",
                 "почему так", "не подходит", "сомнева")
_OBJECTION_EN = ("incorrect", "wrong", "not true", "disagree", "mistake",
                 "invalid", "not right", "doesn't match", "does not match")
_QUESTION_RU = ("почему", "зачем", "откуда", "разве", "что значит", "правда ли",
                "как так", "чем обоснован")
_QUESTION_EN = ("why", "how come", "what does", "where did", "is it true",
                "really", "what is the reason")


def _has_prefix(text: str, stems: tuple[str, ...]) -> bool:
    return any(text.startswith(s) for s in stems)


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    """Совпадение по границам слов (без ложных «add» в «address»)."""
    for w in words:
        if " " in w:
            if w in text:
                return True
        elif re.search(rf"(?<![a-zа-яё0-9_]){re.escape(w)}(?![a-zа-яё0-9_])", text):
            return True
    return False


@dataclass(frozen=True)
class Intent:
    kind: str            # changes | objection | question | clarify
    reason: str          # что именно сработало (для логов/тестов)

    @property
    def needs_rewrite(self) -> bool:
        return self.kind == CHANGES


def classify_comment(body: str) -> Intent:
    """Намерение комментария по тексту. Пустой текст -> clarify (не гадаем).

    Разбираем по клаузам (запятая/точка с запятой): в русской ревью-речи нормальна
    связка «неверно, добавь пример», где просьба стоит во второй клаузе.
    """
    low = " ".join((body or "").lower().split())
    if not low:
        return Intent(CLARIFY, "пустой комментарий")
    clauses = [c.strip() for c in re.split(r"[,;.!]\s*", low) if c.strip()] or [low]

    # 1. явная просьба изменить — сильнее остальных сигналов
    for clause in clauses:
        m = _IMPERATIVE_RE.match(clause)
        if m:
            return Intent(CHANGES, f"просьба в начале клаузы: «{m.group(0).strip()}»")
        hit = next((w for w in _CHANGE_PHRASE_RU if w in clause), None)
        if hit:
            return Intent(CHANGES, f"просьба изменить (ru: «{hit}»)")
    hit = next((w for w in _CHANGE_PHRASE_EN if w in low), None)
    if hit:
        return Intent(CHANGES, f"просьба изменить (en: «{hit}»)")

    # 2. возражение: содержимое неверно -> бот объясняет или исправляет
    for words, tag in ((_OBJECTION_RU, "ru"), (_OBJECTION_EN, "en")):
        hit = next((w for w in words if w in low), None)
        if hit:
            return Intent(OBJECTION, f"возражение ({tag}: «{hit}»)")

    # 3. вопрос — отвечаем, но не переписываем
    if low.endswith("?") or _has_prefix(low, _QUESTION_RU) \
            or _has_word(low, _QUESTION_EN):
        return Intent(QUESTION, "вопрос")

    # 4. непонятный комментарий: спрашиваем, что поправить (без переписывания)
    return Intent(CLARIFY, "нет явной просьбы изменить")


# --------------------------------------------------------------------- ответы --
def _files_lines(state: dict) -> str:
    files = (state.get("payload") or {}).get("files", [])
    return "\n".join(f"- `{f.get('path', '?')}` (target: {f.get('target', '?')})"
                     for f in files) or "- (файлы не определены)"


def reply_for(intent: Intent, state: dict, comment: dict) -> str:
    """Текст ответа бота в тред PR. Никогда не пустой — комментарий без реакции
    ломает доверие к боту (решение заказчика)."""
    author = comment.get("author") or "ревьюер"
    files = _files_lines(state)
    pr_ref = state.get("pr_ref", "?")

    if intent.kind == CHANGES:
        return (f"@{author}, принял: переписываю черновик с учётом замечания.\n\n"
                f"Файлы, которые обновлю:\n{files}\n\n"
                "_Обновление появится в этом же PR — отдельный PR не создаётся._")
    if intent.kind == OBJECTION:
        targets = ", ".join(state.get("expected_targets") or []) or "не определены"
        return (f"@{author}, спасибо за замечание — разберусь по существу.\n\n"
                f"На что опирался черновик: изменения в PR `{pr_ref}`, "
                f"целевые документы: {targets}. Если правка нужна — напишите, "
                "что именно поправить, и я обновлю тот же PR.")
    if intent.kind == QUESTION:
        return (f"@{author}, отвечаю: черновик построен по правилам триггеров из "
                f"diff-а PR `{pr_ref}` и затрагивает только эти документы:\n{files}\n\n"
                "Если нужен другой акцент — скажите, и я перепишу.")
    return (f"@{author}, уточните, пожалуйста, что поправить (что добавить, "
            f"переформулировать или убрать) — сейчас затронуты:\n{files}")
