"""Дешёвые текстовые эвристики поверх diff (без LLM) — «второй слой» триггеров.

Зачем (см. docs/reports/step1_gold_metrics.md):
детерминированный AST-дифф + файловые классификаторы покрывают `api_*`, `adr_*`
(по путям), `dependency_added`, `ci_test_only`, `docs_only`, `typo_fix`,
`style_format_only`. Они **не покрывают** три группы, и без них recall на gold-сете
обваливается до 0.45:

  * `new_user_feature` — по §6 плана шага 0 это решение LLM (шаг 2). Здесь живёт
    rule-fallback той же формы, что `verify._rule_fallback`: он даёт сигнал уже на
    шаге 1 и помечает `needs_llm=True`, чтобы шаг 2 заменил эвристику моделью,
    не меняя контракт инструмента;
  * `config_or_env_changed` — env-имена в pydantic-настройках (`class Settings
    (BaseSettings)` с `env_prefix`) выводятся из имени поля, а не из `os.environ`;
    плюс ключи сборки в `pyproject.toml` (`requires-python`) и значения опций
    (`log_level = "JSON"`);
  * `behavior_changed` — правка дефолта параметра или тела однострочного
    объявления остаётся «невидимой» для `_decl_lines`-подхода к диффу тел.

Все функции чистые и детерминированные. LLM здесь не вызывается никогда.
"""

from __future__ import annotations

import re

from docagent.mcp_server.analyzers.diff_parser import (
    ParsedFile,
    is_code_path,
    is_doc_path,
    is_test_path,
)

# --------------------------------------------------------------------------- #
# new_user_feature (rule-fallback до подключения LLM на шаге 2)
# --------------------------------------------------------------------------- #
_DEF_RE = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:async\s+)?def\s+(?!_)([A-Za-z]\w*)")
_CLASS_RE = re.compile(r"^\s*class\s+(?!_)([A-Z][A-Za-z0-9_]*)")
_EXAMPLE_PATH_RE = re.compile(r"(^|/)(examples?|samples?|tutorials?|demos?)/|_example\.py$")
_CLI_HINT_RE = re.compile(r"\b(argparse|ArgumentParser|click|typer|console_scripts|entry_points)\b")
_USER_DOC_RE = re.compile(
    r"(getting[- _]?started|quickstart|user[- _]?guide|tutorial|how[- _]to|"
    r"пользовател\w+|руководств\w+|быстрый старт)",
    re.I,
)
_USER_FACING_NAME_RE = re.compile(
    r"(Client|Session|Policy|Manager|Pipeline|Backend|Runner|Classifier|Predictor|"
    r"Loader|Exporter|Server|Config|Settings|Application)"
)


def _added_defs(files: list[ParsedFile]) -> dict[str, list[str]]:
    """Новые/изменённые def/class по добавленным строкам.

    Важно: в diff-строках есть префикс `+`, поэтому срезается leading `+` и
    пробелы перед матчем (gold-009/016/049 иначе не давали defs вообще).
    """
    out: dict[str, list[str]] = {}
    for f in files:
        if not is_code_path(f.path) or is_test_path(f.path):
            continue
        names: list[str] = []
        for line in f.added_lines:
            st = line.lstrip("+ ").rstrip()
            m = _DEF_RE.match(st) or _CLASS_RE.match(st)
            if m:
                names.append(m.group(1))
        if names:
            out[f.path] = names
    return out


def detect_new_user_feature(parsed: list[ParsedFile]) -> tuple[str, str, str, int] | None:
    """(file, symbol, detail, score) самого «пользовательского» нового символа.

    Доказательства пользовательскости, по убыванию веса: runnable-пример в PR,
    реэкспорт из корня пакета (`demopkg/__init__.py` / `__all__`), CLI/entry points,
    новый пользовательский раздел документации, публичный протокол (`__enter__`),
    «сервисное» имя символа.
    """
    defs = _added_defs(parsed)
    if not defs:
        return None

    has_examples = any(_EXAMPLE_PATH_RE.search(f.path) for f in parsed)
    blob_all = "\n".join(l for f in parsed for l in f.added_lines)
    has_cli = bool(_CLI_HINT_RE.search(blob_all))
    user_docs = any(
        _USER_DOC_RE.search("\n".join(f.added_lines))
        for f in parsed
        if is_doc_path(f.path) or f.path.lower() == "readme.md"
    )
    exported: set[str] = set()
    for f in parsed:
        if not f.path.endswith("__init__.py"):
            continue
        for line in f.added_lines:
            exported.update(re.findall(r"[\"']([A-Za-z]\w*)[\"']", line))
            m = re.search(r"import\s+(?:([\w, ]+?)|\(([^)]*)\))\s*(?:#.*)?$", line)
            for grp in (m.groups() if m else ()):
                if grp:
                    exported.update(x.strip().split(" as ")[-1] for x in grp.split(",") if x.strip())
    has_protocol = any(
        re.match(r"\s*def\s+__(?:enter|exit|call|iter|next)__", l)
        for f in parsed
        if is_code_path(f.path)
        for l in f.added_lines
    )

    best: tuple[int, str, str, str] | None = None
    for path, names in sorted(defs.items()):
        for name in names:
            why: list[str] = []
            score = 0
            if has_examples:
                score += 3
                why.append("PR содержит examples/ — фича показана пользователю")
            if name in exported:
                score += 3
                why.append("символ реэкспортирован из пакета (__init__/__all__)")
            if has_cli:
                score += 2
                why.append("изменён CLI / entry points")
            if user_docs:
                score += 2
                why.append("добавлен пользовательский раздел документации")
            if has_protocol:
                score += 2
                why.append("реализован пользовательский протокол (context manager)")
            if _USER_FACING_NAME_RE.search(name):
                score += 1
                why.append("имя символа указывает на пользовательский интерфейс")
            if score < 2:  # нужен хотя бы один сильный признак
                continue
            cand = (score, path, name, "; ".join(why))
            if best is None or cand[0] > best[0]:
                best = cand
    if best is None:
        return None
    score, path, name, detail = best
    return path, name, f"{name}: {detail}", score


# --------------------------------------------------------------------------- #
# config_or_env_changed
# --------------------------------------------------------------------------- #
_SETTINGS_CLASS_RE = re.compile(r"class\s+[A-Z]\w*\s*\([^)]*\bBaseSettings\b[^)]*\)\s*:")
_ENV_PREFIX_RE = re.compile(r"""env_prefix\s*=\s*["']([A-Za-z0-9_]+)["']""")
_FIELD_RE = re.compile(r"^(\s{2,})([a-z_][a-z0-9_]*)\s*:\s*([^=#]*?)=\s*([^#\n]*)\s*(?:#\s*(.*))?$")
_NEW_FIELD_RE = re.compile(r"^\s{2,}([a-z_][a-z0-9_]*)\s*:")
_PYPROJECT_KEY_RE = re.compile(
    r"^(requires-python|build-backend|requires|python_requires|license|dependencies)\b"
)
_TOML_VALUE_RE = re.compile(r"""^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$""")
_SEMANTIC_VALUES = {"json", "text", "true", "false", "none"}


def _fields(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for l in lines:
        m = _FIELD_RE.match(l)
        if m:
            out[m.group(2)] = (m.group(3) + "=" + (m.group(4) or "") + "#" + (m.group(5) or "")).strip()
    return out


def detect_config_env(parsed: list[ParsedFile]) -> tuple[str, list[str], str] | None:
    """(file, vars, detail): env-переменные и конфигурационные ключи, затронутые PR."""
    hits: list[str] = []
    file_used = ""
    detail_bits: list[str] = []

    for f in parsed:
        base = f.path.split("/")[-1]
        blob = "\n".join(f.removed_lines + f.added_lines)

        # 1) pydantic BaseSettings: поле → ENV_VAR (с учётом env_prefix)
        if is_code_path(f.path) and _SETTINGS_CLASS_RE.search(blob):
            pm = _ENV_PREFIX_RE.search(blob)
            prefix = (pm.group(1) if pm else "").rstrip("_").upper()
            if not prefix and base in {"settings.py", "config.py"}:
                pkg = f.path.split("/")[0].replace("-", "_").upper()
                prefix = pkg
            before, after = _fields(f.removed_lines), _fields(f.added_lines)
            new_names = [
                m.group(1)
                for m in map(_NEW_FIELD_RE.match, f.added_lines)
                if m and m.group(1) not in before
            ]
            changed_names = [k for k in after if k in before and after[k] != before[k]]
            names = sorted(set(new_names) | set(changed_names))
            if names:
                file_used = file_used or f.path
                var_names = [f"{prefix}_{n.upper()}" if prefix else n.upper() for n in names]
                hits.extend(var_names)
                kind = "новые поля настроек" if new_names else "изменены поля настроек"
                detail_bits.append(f"{kind}: {var_names}")

        # 2) явные os.environ / getenv в добавленных строках
        if is_code_path(f.path):
            for line in f.added_lines:
                m = re.search(r"""(?:os\.environ(?:\.get)?|getenv)\s*[\[\(]\s*["']([A-Z][A-Z0-9_]{2,})["']""", line)
                if m:
                    file_used = file_used or f.path
                    hits.append(m.group(1))
                    detail_bits.append(f"чтение env: {m.group(1)}")

        # 3) ключи сборки в pyproject/setup
        if base in {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini"}:
            b_map, a_map = {}, {}
            for l in f.removed_lines:
                mm = _TOML_VALUE_RE.match(l)
                if mm:
                    b_map[mm.group(1)] = mm.group(2).strip()
            for l in f.added_lines:
                mm = _TOML_VALUE_RE.match(l)
                if mm:
                    a_map[mm.group(1)] = mm.group(2).strip()
            for key in sorted(set(a_map) & set(b_map)):
                if a_map[key] != b_map[key] and _PYPROJECT_KEY_RE.match(key):
                    file_used = file_used or f.path
                    hits.append(key)
                    detail_bits.append(f"ключ сборки {key}: {b_map[key]} → {a_map[key]}")
            for key in sorted(set(a_map) - set(b_map)):
                if _PYPROJECT_KEY_RE.match(key) and key != "dependencies":
                    file_used = file_used or f.path
                    hits.append(key)
                    detail_bits.append(f"новый ключ сборки {key}={a_map[key]}")

        # 4) семантическое значение опции в конфиге (.env.example, *.ini/cfg, settings.py комментарий)
        if base.startswith(".env") or base.endswith((".ini", ".cfg")) or is_doc_path(f.path):
            b_map = {m.group(1).upper(): m.group(2) for m in
                    (_TOML_VALUE_RE.match(l) for l in f.removed_lines) if m}
            a_map = {m.group(1).upper(): m.group(2) for m in
                     (_TOML_VALUE_RE.match(l) for l in f.added_lines) if m}
            for key in sorted(set(a_map) & set(b_map)):
                if a_map[key].strip(' "', "'").lower() != b_map[key].strip(' "', "'").lower():
                    file_used = file_used or f.path
                    hits.append(key)
                    detail_bits.append(f"значение опции {key}: {b_map[key]} → {a_map[key]}")

        # 5) комментарий у поля настройки явно описывает новое допустимое значение
        if is_code_path(f.path):
            for l in f.added_lines:
                cm = re.match(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\b(.*)$", l)
                if cm and len(cm.group(2)) > 8:
                    file_used = file_used or f.path
                    hits.append(cm.group(1))
                    detail_bits.append(f"документировано назначение {cm.group(1)}: {cm.group(2).strip()[:70]}")

    uniq = sorted(set(hits))
    if not uniq:
        return None
    return file_used or (parsed[0].path if parsed else ""), uniq, "; ".join(detail_bits)[:400]


# --------------------------------------------------------------------------- #
# release-notes (CHANGELOG): релиз с breaking/API-изменениями
# --------------------------------------------------------------------------- #
_RELEASE_HEAD_RE = re.compile(r"^\s*##\s+\[?\d+\.\d+", re.M)
_BREAKING_RE = re.compile(r"breaking", re.I)


def detect_release_notes(parsed: list[ParsedFile]) -> tuple[str, str] | None:
    """Новая секция релиза в CHANGELOG с API-изменениями → писать README Release notes."""
    for f in parsed:
        base = f.path.split("/")[-1].lower()
        if "changelog" not in base and "history" not in base:
            continue
        added = "\n".join(f.added_lines)
        if _RELEASE_HEAD_RE.search(added) and _BREAKING_RE.search(added):
            return f.path, "в changelog добавлен релиз с breaking/API-изменениями"
    return None


# --------------------------------------------------------------------------- #
# build/ops-конфигурация: Makefile, CI, dev-requirements
# --------------------------------------------------------------------------- #
_MAKE_TARGET_RE = re.compile(r"^(?:export\s+)?([A-Za-z][A-Za-z0-9_-]*):", re.M)
_DEPS_LINE_RE = re.compile(r"^[A-Za-z0-9_.\-]+(?:[<>!=~]=?[^\s]*)?$")
_PYPROJECT_TABLE_RE = re.compile(r"^\s*\[([^]\[]+)\]")
_OPS_COMMENT_RE = re.compile(r"#\s*([A-Z][A-Z0-9_]{2,})\b(.{8,})$")


def _is_dependency_line(s: str) -> bool:
    if not s or s.startswith(("#", "[")) or "=" in s[:1] or " " in s:
        return False
    return bool(_DEPS_LINE_RE.match(s)) and not s.endswith(":")


def detect_build_ops(parsed: list[ParsedFile]) -> tuple[str, list[str], str] | None:
    """Изменения в инфраструктуре сборки/разработки: цели Makefile, dev-зависимости, таблицы pyproject.

    Это `config_or_env_changed` в широком смысле (§6 плана: конфигурация проекта),
    а не только env-переменные: документатор обязан обновить CONTRIBUTING/README,
    когда меняется способ собрать/запустить окружение.
    """
    hits: list[str] = []
    file_used = ""
    details: list[str] = []
    for f in parsed:
        base = f.path.split("/")[-1]
        if base == "Makefile" or base.endswith("-make.mk"):
            before = {m.group(1) for m in _MAKE_TARGET_RE.finditer("\n".join(f.removed_lines))}
            after = [m.group(1) for m in _MAKE_TARGET_RE.finditer("\n".join(f.added_lines))]
            new_targets = [t for t in after if t not in before]
            if new_targets:
                file_used = file_used or f.path
                hits.extend(new_targets)
                details.append(f"новые цели Makefile: {new_targets}")
            for l in f.added_lines:
                cm = _OPS_COMMENT_RE.search(l.strip())
                if cm:
                    hits.append(cm.group(1))
                    details.append(f"документированная опция: {cm.group(1)}{cm.group(2)[:60]}")
        elif base in {"requirements-dev.txt", "requirements.txt"}:
            before = {l.strip().split("=")[0].split(">")[0] for l in f.removed_lines if _is_dependency_line(l.strip())}
            new_deps = [
                l.strip() for l in f.added_lines
                if _is_dependency_line(l.strip()) and l.strip().split("=")[0].split(">")[0] not in before
            ]
            if new_deps:
                file_used = file_used or f.path
                hits.extend(new_deps)
                details.append(f"новые dev-зависимости: {new_deps}")
        elif base == "pyproject.toml":
            before = {m.group(1) for m in _PYPROJECT_TABLE_RE.finditer("\n".join(f.removed_lines))}
            after = [m.group(1) for m in _PYPROJECT_TABLE_RE.finditer("\n".join(f.added_lines))]
            new_tables = [t for t in after if t not in before and not t.startswith("tool.")]
            if new_tables:
                file_used = file_used or f.path
                hits.extend(new_tables)
                details.append(f"новые секции pyproject: {new_tables}")
    if not hits:
        return None
    return file_used, sorted(set(hits)), "; ".join(details)[:400]


# --------------------------------------------------------------------------- #
# behavior_changed: default-only правки и однострочные тела
# --------------------------------------------------------------------------- #
_DECL_FULL_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)\s*(\(.*\))?\s*(:.*)?$")


def detect_behavior_extra(parsed: list[ParsedFile]) -> tuple[str, str, str] | None:
    """Дополняет `api_diff.behavior_delta` двумя случаями, которые тот пропускает.

    A) правка только значений по умолчанию внутри объявления (retries=3→5):
       `_decl_lines` относит строку к объявлениям, поэтому дельта тел пустая;
    B) однострочное объявление с телом (`def f(): return x`) изменилось целиком.
    """
    # A) defaults inside declarations
    for f in parsed:
        if not is_code_path(f.path):
            continue
        before = {_DECL_FULL_RE.match(l).group(1): l for l in f.removed_lines if _DECL_FULL_RE.match(l)}
        after = {_DECL_FULL_RE.match(l).group(1): l for l in f.added_lines if _DECL_FULL_RE.match(l)}
        for name in sorted(set(before) & set(after)):
            b, a = before[name], after[name]
            sig_b = re.sub(r"=\s*[^,()]+", "=", b)
            sig_a = re.sub(r"=\s*[^,()]+", "=", a)
            if sig_b == sig_a and b != a:
                db = dict(re.findall(r"(\w+)\s*=\s*([^,()\s]+)", b))
                da = dict(re.findall(r"(\w+)\s*=\s*([^,()\s]+)", a))
                changed = [k for k in db if k in da and db[k] != da[k]]
                if changed:
                    det = ", ".join(f"{k}: {db[k]} → {da[k]}" for k in changed)
                    return f.path, name, f"дефолт(ы) {name}({det}) изменены без смены сигнатуры"

    # B) inline bodies
    inline = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)\s*\(?.*?\)?\s*:\s+\S")
    for f in parsed:
        if not is_code_path(f.path):
            continue
        before = {m.group(1): l.strip() for l in f.removed_lines if (m := inline.match(l))}
        after = {m.group(1): l.strip() for l in f.added_lines if (m := inline.match(l))}
        for name in sorted(set(before) & set(after)):
            b, a = before[name], after[name]
            if b.split(":", 1)[0] == a.split(":", 1)[0] and b != a:
                return f.path, name, f"тело {name} изменено в однострочном объявлении: `{b}` → `{a}`"
    return None
