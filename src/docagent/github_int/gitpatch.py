"""Нормализация diff'ов, чтобы их принимал `git apply`.

Gold-сет (шаг 0) сгенерирован с двумя дефектами формата:
  1. нет заголовков `diff --git a/… b/…` — сразу идёт `--- a/file` / `+++ b/file`;
  2. счётчики в hunk-заголовках не соответствуют телу: например
     `@@ -10,7 +10,9 @@`, а строк в hunk'е 4/10. Детектор правил счётчики игнорирует
     (`diff_parser` читает только контекст), поэтому метрики шага 1 от этого не
     страдали — а `git apply` отказывается: «corrupt patch at line N».

`normalize_patch` чинит оба дефекта, не меняя смысл diff'а: содержимое строк и
порядок hunk'ов остаются, пересчитываются только счётчики — ровно то, что делает
`git apply --recount`. Функция идемпотентна.
"""
from __future__ import annotations

import re

_NEW_FILE_RE = re.compile(r"^--- (?:a/)?(?P<path>[^\t\n]+?)\s*$")
_PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>[^\t\n]+?)\s*$")
_DIFF_GIT_RE = re.compile(r"^diff --git ")
_HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? "
                      r"\+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@(?P<ctx>.*)$")
_DEV_NULL = "/dev/null"


def has_git_header(patch: str) -> bool:
    return any(_DIFF_GIT_RE.match(line) for line in patch.splitlines())


def _file_path_at(lines: list[str], i: int) -> str | None:
    """Путь файла из пары `--- …` / `+++ …` начиная с позиции i.

    Для новых файлов `--- /dev/null`, для удалённых `+++ /dev/null`: берём ту
    половину пары, где путь настоящий. Иначе заголовок получался бы вида
    `diff --git a//dev/null b//dev/null` (проверено на gold-045).
    """
    old = _NEW_FILE_RE.match(lines[i])
    new = _PLUS_FILE_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
    old_path = old.group("path").strip() if old else None
    new_path = new.group("path").strip() if new else None
    if old_path and old_path != _DEV_NULL:
        return old_path
    if new_path and new_path != _DEV_NULL:
        return new_path
    return None


def _with_git_headers(lines: list[str]) -> list[str]:
    """Добавляет `diff --git a/path b/path` перед каждой парой `--- …`/`+++ …`."""
    out: list[str] = []
    current: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _NEW_FILE_RE.match(line):
            path = _file_path_at(lines, i)
            if path and path != current:  # новый файл: ровно один заголовок
                current = path
                out.append(f"diff --git a/{path} b/{path}")
        out.append(line)
        i += 1
    return out


def _recount(lines: list[str]) -> list[str]:
    """Пересчитывает счётчики hunk'ов по их телу (аналог `git apply --recount`).

    Попутно чинит заголовок-гибрид `@@ new file mode 100644` (генератор gold-сета
    слепил режим файла с `@@`) и выбрасывает заголовок без единой строки тела —
    иначе git ругается «patch with only garbage».
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HUNK_RE.match(line)
        if m is None and line.startswith("@@"):
            # тело, чтобы понять, новый это файл или правка существующего
            j = i + 1
            body_lines: list[str] = []
            while j < len(lines) and not lines[j].startswith(
                    ("@@", "diff --git ", "--- ", "+++ ")):
                body_lines.append(lines[j])
                j += 1
            has_old = any(bl.startswith((" ", "-")) and not bl.startswith("---")
                          for bl in body_lines)
            if body_lines and not has_old:
                out.append(f"@@ -0,0 +1,{len(body_lines)} @@")
                out.extend(body_lines)
            elif body_lines:
                old = sum(1 for bl in body_lines if not bl.startswith("+"))
                new = sum(1 for bl in body_lines if not bl.startswith("-"))
                out.append(f"@@ -1,{old} +1,{new} @@")  # точные старты неизвестны
                out.extend(body_lines)
            # hunk без тела просто теряется: он ничего не меняет
            i = j
            continue
        if m is None:
            out.append(line)
            i += 1
            continue
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if _HUNK_RE.match(nxt) or _NEW_FILE_RE.match(nxt) \
                    or _DIFF_GIT_RE.match(nxt) or nxt.startswith("--- ") \
                    or nxt.startswith("+++ ") or nxt.startswith("@@"):
                break
            body.append(nxt)
            j += 1
        old = new = 0
        for bl in body:
            if bl.startswith("\\"):      # "\ No newline at end of file"
                continue
            if bl.startswith("+"):
                new += 1
            elif bl.startswith("-"):
                old += 1
            else:
                old += 1
                new += 1
        if not body:
            i = j  # пустой hunk не нужен: git на нём падает
            continue
        out.append(f"@@ -{m.group('old_start')},{old} "
                   f"+{m.group('new_start')},{new} @@{m.group('ctx')}")
        out.extend(body)
        i = j
    return out


def count_mismatches(patch: str) -> list[str]:
    """Расхождения счётчиков hunk'ов (для аудита сета и тестов)."""
    problems: list[str] = []
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        m = _HUNK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        need_old = int(m.group("old_len") or 1)
        need_new = int(m.group("new_len") or 1)
        got_old = got_new = 0
        i += 1
        while i < len(lines):
            line = lines[i]
            if _HUNK_RE.match(line) or _NEW_FILE_RE.match(line) \
                    or _DIFF_GIT_RE.match(line):
                break
            if not line.startswith("\\"):
                if line.startswith("+"):
                    got_new += 1
                elif line.startswith("-"):
                    got_old += 1
                else:
                    got_old += 1
                    got_new += 1
            i += 1
        if (got_old, got_new) != (need_old, need_new):
            problems.append(f"hunk @{m.group('old_start')}: заявлено "
                            f"({need_old},{need_new}), в теле ({got_old},{got_new})")
    return problems


def normalize_patch(patch: str, *, recount: bool = True) -> str:
    """Возвращает diff, пригодный для `git apply` (заголовки + верные счётчики)."""
    if not patch.strip():
        return patch
    lines = patch.splitlines()
    if not has_git_header(patch):
        lines = _with_git_headers(lines)
    if recount:
        lines = _recount(lines)
    return "\n".join(lines) + "\n"


def repair_patch(patch: str) -> str:
    """Как `normalize_patch`, но пересобирает и уже существующие заголовки.

    Нужно для починки данных, испорченных ранней версией нормализации: для новых
    файлов она писала `diff --git a//dev/null b//dev/null`, а `normalize_patch`
    готовые заголовки не трогает.
    """
    if not patch.strip():
        return patch
    lines = [ln for ln in patch.splitlines() if not _DIFF_GIT_RE.match(ln)]
    lines = _recount(_with_git_headers(lines))
    return "\n".join(lines) + "\n"
