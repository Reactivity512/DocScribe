"""Детерминированный дифф публичного API между двумя снимками (ревизиями) кода.

Работает в двух режимах:
  * full — есть оба дерева файлов (git ref/папка): сравниваем AST целиком;
  * patch — есть только diff: извлекаем удалённые строки (`-`) как «до» и
    добавленные (`+`) как «после», парсим их как фрагменты модуля. Точность ниже,
    но для gold-кейсов с inline-diff этого достаточно.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from docagent.mcp_server.analyzers.diff_parser import ParsedFile, is_code_path
from docagent.mcp_server.analyzers.python_ast import signature_str, symbols_from_source
from docagent.mcp_server.models import ApiChangeKind, SignatureParam, SymbolInfo


@dataclass
class ApiChangeRecord:
    """Внутренняя запись изменения символа (с флагом публичности)."""

    kind: ApiChangeKind
    symbol: str
    file: str
    old_signature: str | None = None
    new_signature: str | None = None
    param_diff: dict = field(default_factory=dict)
    breaking: bool = False
    reasons: list[str] = field(default_factory=list)
    symbol_public: bool = True


def _norm_default(d: str | None) -> str | None:
    if d is None:
        return None
    try:
        return ast.unparse(ast.parse(d.strip("()")))
    except Exception:
        return d.strip()


def params_map(params: list[SignatureParam]) -> dict[str, tuple[str | None, str | None]]:
    return {p.name: (p.annotation, _norm_default(p.default)) for p in params}


def compare_params(old: list[SignatureParam], new: list[SignatureParam]) -> tuple[dict, bool, list[str]]:
    om, nm = params_map(old), params_map(new)
    added = sorted(set(nm) - set(om))
    removed = sorted(set(om) - set(nm))
    default_changed, annotation_changed = [], []
    for k in sorted(set(om) & set(nm)):
        if om[k][1] != nm[k][1]:
            default_changed.append(k)
        if om[k][0] != nm[k][0]:
            annotation_changed.append(k)
    # аннотация `self`/`cls` — служебная, не контракт пользователя
    annotation_changed = [k for k in annotation_changed if k not in ("self", "cls")]
    breaking, reasons = False, []
    if removed:
        breaking = True
        reasons.append(f"удалены параметры: {removed}")
    positional_new = [a for a in added if not a.startswith("*")]
    first_required = next(
        (a for a in positional_new if nm[a][1] is None), None
    )
    if first_required:
        breaking = True
        reasons.append(f"обязательный позиционный параметр {first_required!r}")
    elif positional_new:
        reasons.append(f"добавлены опциональные параметры: {positional_new}")
    for k in default_changed:
        reasons.append(f"дефолт {k}: {om[k][1]} → {nm[k][1]}")
    for k in annotation_changed:
        reasons.append(f"аннотация {k}: {om[k][0]} → {nm[k][0]}")
    if breaking or default_changed or annotation_changed:
        breaking = breaking or bool(default_changed)
    diff = {
        "added": added,
        "removed": removed,
        "default_changed": default_changed,
        "annotation_changed": annotation_changed,
    }
    return diff, breaking, reasons


# --------------------------------------------------------------------------- #
# Режим full: два дерева файлов
# --------------------------------------------------------------------------- #
def snapshot_files(files: dict[str, str], root: Path | None = None) -> tuple[list[SymbolInfo], list[str]]:
    syms: list[SymbolInfo] = []
    errors: list[str] = []
    for path, src in sorted(files.items()):
        if not is_code_path(path):
            continue
        s, _, e = symbols_from_source(src, path, root)
        syms.extend(s)
        errors.extend(e)
    return syms, errors


def diff_snapshots(before: list[SymbolInfo], after: list[SymbolInfo]) -> list[ApiChangeRecord]:
    bm = {(s.file, s.qualified_name): s for s in before}
    am = {(s.file, s.qualified_name): s for s in after}
    out: list[ApiChangeRecord] = []
    for key in sorted(set(bm) | set(am), key=lambda k: (k[0], k[1])):
        b, a = bm.get(key), am.get(key)
        file, sym = key
        if b and not a:
            out.append(ApiChangeRecord(
                kind=ApiChangeKind.removed, symbol=sym, file=file,
                old_signature=b.signature, symbol_public=b.public,
                breaking=b.public, reasons=["публичный символ удалён"] if b.public else [],
            ))
        elif a and not b:
            out.append(ApiChangeRecord(
                kind=ApiChangeKind.added, symbol=sym, file=file,
                new_signature=a.signature, symbol_public=a.public,
            ))
        elif b and a:
            pdiff, breaking, reasons = compare_params(b.params, a.params)
            changed_doc = b.has_docstring != a.has_docstring
            # dunder-протокол (__enter__/__exit__/__call__) — пользовательский
            # контракт даже при «приватном» имени (gold-009): считаем публичным
            dunder_public = bool(re.match(r".*\.__\w+__$", sym))
            if dunder_public:
                breaking = True
                reasons = list(reasons) + [
                    "изменён протокол dunder-метода — контракт для пользователей"
                ]
            if pdiff["added"] or pdiff["removed"] or pdiff["default_changed"] or pdiff["annotation_changed"]:
                out.append(ApiChangeRecord(
                    kind=ApiChangeKind.changed, symbol=sym, file=file,
                    old_signature=b.signature, new_signature=a.signature,
                    param_diff=pdiff, breaking=breaking, reasons=reasons,
                    symbol_public=a.public or b.public or dunder_public,
                ))
            elif b.signature != a.signature:
                out.append(ApiChangeRecord(
                    kind=ApiChangeKind.changed, symbol=sym, file=file,
                    old_signature=b.signature, new_signature=a.signature,
                    param_diff={}, breaking=breaking,
                    reasons=(reasons or ["сигнатура изменилась (порядок/звёздочки)"]),
                    symbol_public=a.public or b.public or dunder_public,
                ))
            elif changed_doc:
                out.append(ApiChangeRecord(
                    kind=ApiChangeKind.unchanged, symbol=sym, file=file,
                    new_signature=a.signature, symbol_public=a.public,
                    reasons=["docstring добавлен/убран — не является изменением контракта"],
                ))
    return out


# --------------------------------------------------------------------------- #
# Режим patch: только diff
# --------------------------------------------------------------------------- #
def _fragment_symbols(lines: list[str], file: str) -> dict[str, SymbolInfo]:
    """Парсинг набора строк diff как фрагмента модуля (tolerant).

    Сначала пробуем собрать все строки вместе (класс + методы), затем — каждую
    строку-объявление отдельно. Отступы из diff сохраняем: `ast.parse` их терпит,
    если блок начинается с той же колонки, что и заголовок.
    """
    src = "\n".join(lines)
    syms, _, _ = symbols_from_source(src, file)
    if not syms:
        for l in lines:
            st = l.strip()
            if not st:
                continue
            indent = len(l) - len(l.lstrip())
            candidate = st + "\n" + " " * (indent + 4) + "pass"
            one, _, _ = symbols_from_source(candidate, file)
            for x in one:
                syms.append(x)
    return {(x.qualified_name or x.name): x for x in syms}


def _decl_lines(lines: list[str]) -> list[str]:
    out = []
    for l in lines:
        st = l.strip()
        if st.startswith(("def ", "async def ", "class ", "__all__")):
            out.append(l)
    return out


def _is_public_decl_line(line: str) -> bool:
    st = line.strip()
    m = re.match(r"(?:async\s+)?(?:def|class)\s+([^\s_(,:]+)", st)
    return bool(m) and not m.group(1).startswith("_")


def _norm_decl(line: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Нормализованное объявление символа: (имя, ((param, default), ...)).

    Нужен чтобы отличать **смену контракта** от косметики: добавление
    аннотаций типов или `-> None` не меняет ни имени, ни имён/дефолтов
    параметров. Разбор делаем через `ast` (tolerant: обрезаем return-аннотацию
    и служебные параметры self/cls).
    """
    st = line.strip().rstrip(":")
    if not st:
        return None
    # отрезаем `-> ret`: на контракт он не влияет
    head = _cut_return(st)
    src = head if head.lstrip().startswith(("class ", "def ", "async ")) else head
    try:
        tree = ast.parse(_pad_block(src))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pairs = []
            args = node.args
            posonly = list(args.posonlyargs)
            regular = list(args.args)
            kwonly = list(args.kwonlyargs)
            defaults = [None] * (len(posonly + regular) - len(args.defaults)) + list(args.defaults)
            for arg, dflt in zip(posonly + regular, defaults):
                if arg.arg in ("self", "cls"):
                    continue
                pairs.append((arg.arg, _unparse_default(dflt)))
            for arg, dflt in zip(kwonly, list(args.kw_defaults)):
                pairs.append((arg.arg, _unparse_default(dflt)))
            if args.vararg:
                pairs.append(("*" + args.vararg.arg, ""))
            if args.kwarg:
                pairs.append(("**" + args.kwarg.arg, ""))
            return node.name, tuple(pairs)
        if isinstance(node, ast.ClassDef):
            return node.name, ()
    return None


def _unparse_default(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _cut_return(st: str) -> str:
    """Убирает `-> <rettype>` с конца объявления (скобки учитываем)."""
    depth = 0
    i = 0
    while i < len(st) - 1:
        c = st[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c == "-" and st[i + 1] == ">":
            return st[:i].rstrip()
        i += 1
    return st


def _pad_block(src: str) -> str:
    """Делает из строки-объявления разбираемый блок (`def f():` → `def f():\\n pass`)."""
    indent = len(src) - len(src.lstrip())
    body = " " * (indent + 4) + "pass"
    if src.rstrip().endswith(":"):
        return src + "\n" + body
    # без двоеточия (обрезанный diff) — закрываем скобки и добавляем тело
    fixed = src
    opens = sum(fixed.count(c) for c in "([{")
    closes = sum(fixed.count(c) for c in ")]}")
    if opens > closes:
        fixed += ")" * (opens - closes)
    return fixed + ":\n" + body


def _contract_only_annotations_changed(b_line: str, a_line: str) -> bool:
    """True, если объявление изменилось только аннотациями/return-типом."""
    b, a = _norm_decl(b_line), _norm_decl(a_line)
    if not b or not a:
        return False
    return b[0] == a[0] and b[1] == a[1]


def diff_from_parsed_files(parsed: list[ParsedFile]) -> tuple[list[ApiChangeRecord], list[str]]:
    """Дифф публичного API из одного unified diff (режим patch).

    Экономим на полном клонировании репо: «до» = удалённые строки-объявления,
    «после» = добавленные. Если объявление появилось только со стороны `+` —
    это новый символ, если только со стороны `-` — удалённый.
    """
    records: list[ApiChangeRecord] = []
    errors: list[str] = []
    for f in parsed:
        if not is_code_path(f.path):
            continue
        raw_before = _decl_lines(f.removed_lines)
        raw_after = _decl_lines(f.added_lines)

        # Контрактный фильтр (gold-026): добавление аннотаций типов / return-типа
        # не меняет контракт, но `compare_params` считает annotation_changed →
        # ложный api_signature_changed (+ иногда ложное removed из-за того, что
        # AST-фрагмент с методами без класса не парсится). Сопоставляем объявления
        # по имени и выбрасываем пары «до/после», где имена и набор
        # (параметр, дефолт) совпадают — изменились только типы.
        b_by_name: dict[str, str] = {}
        for l in raw_before:
            n = _decl_ident(l)
            if n:
                b_by_name.setdefault(n, l)
        cosmetic: set[str] = set()
        for al in raw_after:
            n = _decl_ident(al)
            bl = b_by_name.get(n)
            if bl is not None and _contract_only_annotations_changed(bl, al):
                cosmetic.add(n)
        before_lines = [l for l in raw_before if _decl_ident(l) not in cosmetic]
        after_lines = [l for l in raw_after if _decl_ident(l) not in cosmetic]

        before = _fragment_symbols(before_lines, f.path)
        after = _fragment_symbols(after_lines, f.path)
        recs = diff_snapshots(list(before.values()), list(after.values()))
        for r in recs:
            r.file = f.path
        records.extend(recs)

        # fallback по тексту строк: AST мог не разобрать фрагмент
        seen = {r.symbol for r in recs}
        b_names = {k for k, v in before.items() if v.public}
        a_names = {k for k, v in after.items() if v.public}
        after_idents = {_decl_ident(l) for l in after_lines}
        for line in before_lines:
            name = _decl_name(line)
            # если то же имя есть среди добавленных объявлений — это правка
            # объявления (аннотации/дефолты), а не удаление символа
            if name and name in after_idents:
                continue
            if name and name not in a_names and name not in seen and _is_public_decl_line(line):
                records.append(ApiChangeRecord(
                    kind=ApiChangeKind.removed, symbol=name, file=f.path,
                    old_signature=line.strip(), symbol_public=True, breaking=True,
                    reasons=["публичный символ удалён (detected by text fallback)"],
                ))
                seen.add(name)
                b_names.add(name)
        for line in after_lines:
            name = _decl_name(line)
            if name and name not in b_names and name not in seen and _is_public_decl_line(line):
                records.append(ApiChangeRecord(
                    kind=ApiChangeKind.added, symbol=name, file=f.path,
                    new_signature=line.strip(), symbol_public=True,
                ))
                seen.add(name)
    return records, errors


_DECL_IDENT_RE = re.compile(r"\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def _decl_ident(line: str) -> str | None:
    """Имя символа из строки-объявления без учёта параметров (для сопоставления
    «до/после» в fallback: `def f(self, x)` и `def f(self, x: int)` — один символ)."""
    m = _DECL_IDENT_RE.match(line)
    return m.group(1) if m else None


def _decl_name(line: str) -> str | None:
    m = re.match(r"\s*(?:async\s+)?(?:def|class)\s+([^\s_(,:]+)", line)
    return m.group(1) if m else None


def behavior_delta(parsed: list[ParsedFile]) -> tuple[bool, str, str]:
    """Тело публичной функции изменилось, а сигнатура — нет → behavior_changed.

    Эвристика по diff-фрагменту (без полного дерева): в `-` и `+` есть исполняемые
    строки, при этом множество строк-объявлений идентично. Отступы нормализуем,
    чтобы reindent-only правки не давали ложный сигнал.
    """
    body_re = _body_stmt_re()
    for f in parsed:
        if not is_code_path(f.path):
            continue
        added_exec = [l.strip() for l in f.added_lines if body_re.search(l)]
        removed_exec = [l.strip() for l in f.removed_lines if body_re.search(l)]
        if not added_exec or not removed_exec:
            continue
        decl_before = _norm_decls(_decl_lines(f.removed_lines))
        decl_after = _norm_decls(_decl_lines(f.added_lines))
        if decl_before != decl_after:
            continue  # это api_signature_changed / api_new, обработано отдельно
        names = sorted({n for n in map(_decl_name, decl_after) if n})
        if not names:
            continue
        changed = sorted(set(added_exec) ^ set(removed_exec))
        return (
            True,
            f"тело {'/'.join(names[:3])} изменено без смены сигнатуры; "
            f"дельта строк: {changed[:4]}",
            f.path,
        )
    return False, "", ""


def _norm_decls(lines: list[str]) -> list[str]:
    return sorted(l.strip().rstrip(":") for l in lines if l.strip())


def _body_stmt_re():
    return re.compile(r"^\s*(?:if|for|while|try|return|raise|with|assert)\b|=\s*\w+\(|\.\w+\(")
