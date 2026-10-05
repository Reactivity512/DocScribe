"""Детерминированный парсер unified diff (без внешних зависимостей).

Даёт: статистику по файлам, классификацию путей (code/doc/test/ci/config),
текстовые дельты (для typo/style-детекции), добавленные зависимости и env-переменные.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

CODE_EXT = {".py", ".pyi"}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
CI_PREFIXES = (".github/workflows/", ".gitlab-ci", "ci/", "jenkins")
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "poetry.lock", "package.json", "environment.yml", ".env.example",
    "Makefile", "Dockerfile",
}
# Файлы объявления зависимостей: requirements*.txt / pyproject / setup.
DEPS_FILE_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
}
# Инфраструктура сборки/разработки: не код, не тесты, не документация.
INFRA_NAMES = {"Makefile", "Dockerfile", "Containerfile", ".pre-commit-config.yaml"}


def is_deps_file(path: str) -> bool:
    """requirements*.txt / pyproject / setup — файлы объявления зависимостей."""
    base = path.split("/")[-1]
    return base in DEPS_FILE_NAMES or base.startswith("requirements-")


def is_infra_path(p: str) -> bool:
    """Инфраструктура проекта: CI-конфиги, Makefile/Dockerfile, dev-requirements, *.yml."""
    base = p.split("/")[-1]
    if is_ci_path(p) or base in INFRA_NAMES or base.startswith("requirements-"):
        return True
    return p.endswith((".yml", ".yaml")) and not is_code_path(p)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


_ROOT_DOC_NAMES = {"readme.md", "changelog.md", "contributing.md", "history.md"}


def is_doc_path(p: str) -> bool:
    """Документация: всё в docs/ + корневые README/CHANGELOG/CONTRIBUTING."""
    if p.startswith("docs/") or "/docs/" in p:
        return True
    return "/" not in p and p.lower() in _ROOT_DOC_NAMES


def is_test_path(p: str) -> bool:
    name = p.split("/")[-1]
    return (
        "/tests/" in f"/{p}" or p.startswith("tests/")
        or name.startswith("test_") or name.endswith("_test.py")
        or name in {"conftest.py", "pytest.ini"}
    )


def is_ci_path(p: str) -> bool:
    return any(p.startswith(x) or f"/{x}" in p for x in CI_PREFIXES)


def is_config_path(p: str) -> bool:
    base = p.split("/")[-1]
    if is_ci_path(p):
        return False
    return base in CONFIG_NAMES or p.endswith((".toml", ".cfg", ".ini"))


def is_code_path(p: str) -> bool:
    return p.endswith(tuple(CODE_EXT)) and not is_test_path(p)


def language_of(p: str) -> str:
    name = p.split("/")[-1]
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return {
        ".py": "python", ".pyi": "python", ".md": "markdown", ".rst": "rst",
        ".toml": "toml", ".yml": "yaml", ".yaml": "yaml", ".txt": "text",
        ".json": "json", ".sh": "shell",
    }.get(ext, "other")


@dataclass
class ParsedFile:
    path: str
    old_path: str | None = None
    additions: int = 0
    deletions: int = 0
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    hunk_headers: list[str] = field(default_factory=list)
    new_file: bool = False
    deleted_file: bool = False
    renamed: bool = False
    binary: bool = False


@dataclass
class DiffParse:
    files: list[ParsedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    def by_path(self) -> dict[str, ParsedFile]:
        return {f.path: f for f in self.files}


def parse_unified_diff(text: str, max_bytes: int | None = None) -> DiffParse:
    """Разбирает unified diff. Толерантен к обрезанным/битым входным данным."""
    parse = DiffParse()
    if max_bytes and len(text.encode()) > max_bytes:
        text = text[:max_bytes]
        parse.truncated = True

    current: ParsedFile | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("diff --git ") or line.startswith("--- "):
            if line.startswith("diff --git "):
                m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
                path = m.group(2) if m else line.split(" b/")[-1]
                old = m.group(1) if m else None
                if current:
                    parse.files.append(current)
                current = ParsedFile(path=path, old_path=old if old != path else None)
                if current.old_path:
                    current.renamed = True
            continue
        if line.startswith("+++ "):
            if current is None:
                current = ParsedFile(path=line[4:].strip().removeprefix("b/"))
            continue
        if line.startswith("new file mode"):
            if current:
                current.new_file = True
            continue
        if line.startswith("deleted file mode"):
            if current:
                current.deleted_file = True
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if current:
                current.binary = True
            continue
        if _HUNK_RE.match(line):
            if current is None:
                parse.errors.append(f"hunk without file header: {line[:80]}")
                continue
            m = _HUNK_RE.match(line)
            ctx = (m.group(5) or "").strip()
            current.hunk_headers.append(ctx or f"{m.group(3)}")
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current.additions += 1
            current.added_lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current.deletions += 1
            current.removed_lines.append(line[1:])

    if current:
        parse.files.append(current)
    return parse


# --------------------------------------------------------------------------- #
# Сигналы высокого уровня из diff
# --------------------------------------------------------------------------- #
_REQ_RE = re.compile(
    r"""^\s*["']([A-Za-z0-9_.\-\[\]<>=!~+]+)(?:\s*[<>=!~]=?\s*[^"']+)?["']""",
    re.X,
)


def extract_added_dependencies(parse: DiffParse) -> list[str]:
    """Имена пакетов, реально **добавленные** в requirements*/pyproject/setup.

    Корректность важнее широты: раньше функция возвращала все строки из
    diff'а файла зависимостей, из-за чего секции coverage/pytest в
    `pyproject.toml` (`source = [...]`, `fail_under = 80`) превращались в
    «новые зависимости» → ложный триггер на чистый CI-PR (gold-031).

    Правила:
      * только файлы объявления зависимостей (`is_deps_file`);
      * для pyproject/setup.cfg — только внутри таблиц `[project] dependencies` /
        optional-dependencies / `[tool.poetry.dependencies]`;
      * имя пакета — до первого спецификатора версии; отбрасываем маркеры
        окружения (`;`, `#`, `-r`, опции pip) и служебные ключи.
    """
    out: set[str] = set()
    for f in parse.files:
        if not is_deps_file(f.path):
            continue
        base = f.path.split("/")[-1]
        if base in {"setup.py", "Pipfile"} or not base.endswith((".toml", ".cfg")):
            # requirements*.txt / setup.py: плоский список по строке на пакет
            for line in f.added_lines:
                s = line.strip().strip("\"'")
                if not s or s.startswith(("#", "-")):
                    continue
                name = re.split(r"[<>=!~;\[\s]", s, 1)[0].strip()
                name = name.lower().replace("_", "-")
                if _looks_like_requirement(name):
                    out.add(name)
            continue
        out.update(_deps_from_toml_or_cfg(f))
    return sorted(out)


_KEY_ONLY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _looks_like_requirement(name: str) -> bool:
    if not name or len(name) < 2:
        return False
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
        return False
    # служебные слова конфигов (source, fail-under, ...) — не пакеты
    return name not in {"source", "fail-under", "fail_under", "omit", "include",
                        "branch", "plugins", "exclude-lines", "show-missing"}


_DEPS_TABLES = {
    "project.dependencies", "project.optional-dependencies",
    "build-system.requires", "tool.poetry.dependencies",
    "tool.poetry.group", "dependency-groups",
}


def _deps_from_toml_or_cfg(f) -> set[str]:
    """Достаём имена пакетов только из dependency-секций toml/cfg."""
    lines = f.added_lines
    tables_in_diff = {t.lower() for t in re.findall(
        r"^\s*\[([^]\[]+)\]", "\n".join(f.removed_lines + lines), re.M)}
    in_table: set[str] = set()
    for t in tables_in_diff:
        tl = t.replace(" ", "")
        if tl in _DEPS_TABLES or tl.startswith(("project.optional-dependencies",
                                                "tool.poetry.group",
                                                "dependency-groups")):
            in_table.add(tl)
    out: set[str] = set()
    current: str | None = None
    for line in lines:
        st = line.strip()
        m = re.match(r"^\[([^]\[]+)\]", st)
        if m:
            current = m.group(1).lower().replace(" ", "")
            continue
        if current is None or not any(current == t or current.startswith(t) for t in in_table):
            continue
        for mm in re.finditer(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)""", st):
            name = mm.group(1).lower().replace("_", "-")
            if _looks_like_requirement(name):
                out.add(name)
        if current.startswith(("tool.poetry.dependencies", "dependency-groups")):
            mm = re.match(r"^([A-Za-z0-9_.-]+)\s*=", st)
            if mm and _looks_like_requirement(mm.group(1).lower()):
                out.add(mm.group(1).lower().replace("_", "-"))
    return out


_ENV_RE = re.compile(r"\b(?:os\.environ(?:\.get)?|getenv)\s*[\[\(]\s*[\"']([A-Z0-9_]{3,})[\"']")


def extract_env_vars(parse: DiffParse) -> list[str]:
    out: set[str] = set()
    for f in parse.files:
        for line in f.added_lines + f.removed_lines:
            out.update(_ENV_RE.findall(line))
    return sorted(out)


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def word_bag(lines: list[str]) -> Counter:
    c: Counter = Counter()
    for l in lines:
        c.update(w.lower() for w in _WORD_RE.findall(l))
    return c


def multiset_delta(added: list[str], removed: list[str]) -> dict:
    """Дельта сумок слов: новые слова (added-removed) и утраченные (removed-added)."""
    a, r = word_bag(added), word_bag(removed)
    new_words = a - r
    lost_words = r - a
    return {
        "new_words": dict(new_words),
        "lost_words": dict(lost_words),
        "new_total": sum(new_words.values()),
        "lost_total": sum(lost_words.values()),
        "unchanged_total": sum((a & r).values()),
    }


def tokens(added: list[str], removed: list[str]) -> tuple[set[str], set[str]]:
    """Множество токенов кода до/после (для style/format детекции)."""
    def toks(lines: list[str]) -> set[str]:
        t: set[str] = set()
        for l in lines:
            t.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.\d+|\d+", l))
        return t

    return toks(added), toks(removed)
