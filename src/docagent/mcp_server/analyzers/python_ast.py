"""AST-извлечение публичного API Python-файлов.

Эвристика публичности (маркеров в коде нет — решение лида по шагу 0):
  public = не начинается с `_` И (явно в `__all__` ИЛИ модуль не приватный)
Если `__all__` объявлен — оно и есть источник истины для модуля.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from docagent.mcp_server.models import SignatureParam, SymbolInfo

_PRIMITIVE_ANN_RE = re.compile(r"^(int|float|str|bool|bytes|complex)$")


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _module_of(path: Path, root: Path) -> str:
    """demopkg/exporters.py -> demopkg.exporters (для qualified_name)."""
    rel = path.relative_to(root) if path.is_absolute() else path
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def extract_all_names(tree: ast.Module) -> set[str] | None:
    """Значения `__all__`, если он статически вычислим."""
    for node in tree.body:
        targets: list[str] = []
        value = None
        if isinstance(node, ast.Assign):
            targets = [_unparse(t) or "" for t in node.targets]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", ""):
            targets = [node.target.id]
            value = node.value
        if "__all__" not in targets or not isinstance(value, (ast.List, ast.Tuple)):
            continue
        out = set()
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
        return out
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[SignatureParam]:
    a = fn.args
    items: list[tuple[ast.arg, ast.expr | None]] = []
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    items += list(zip(pos, defaults))
    dkw = [None] * (len(a.kwonlyargs) - len(a.kw_defaults)) + list(a.kw_defaults)
    items += list(zip(a.kwonlyargs, dkw))
    if a.vararg:
        items.append((a.vararg, None))
    if a.kwarg:
        items.append((a.kwarg, None))

    out: list[SignatureParam] = []
    for arg, default in items:
        prefix = ""
        if a.vararg and arg is a.vararg:
            prefix = "*"
        elif a.kwarg and arg is a.kwarg:
            prefix = "**"
        elif arg in a.kwonlyargs:
            pass
        name = prefix + arg.arg
        out.append(
            SignatureParam(
                name=name,
                annotation=_unparse(arg.annotation),
                default=_unparse(default),
            )
        )
    return out


def signature_str(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parts = []
    for p in _params(fn):
        s = p.name
        if p.annotation:
            s += f": {p.annotation}"
        if p.default:
            s += f"={p.default}"
        parts.append(s)
    ret = _unparse(fn.returns)
    head = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    sig = f"{head} {fn.name}({', '.join(parts)})"
    if ret:
        sig += f" -> {ret}"
    return sig


def _is_public(name: str, all_names: set[str] | None, module_priv: bool) -> bool:
    if name.startswith("_"):
        return False
    if module_priv:
        return False
    if all_names is not None:
        return name in all_names
    return True


def symbols_from_source(source: str, file_path: str, root: Path | None = None) -> tuple[list[SymbolInfo], dict[str, str], list[str]]:
    """Возвращает (символы, {модуль: docstring}, ошибки парсинга)."""
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return [], {}, [f"{file_path}: SyntaxError: {e}"]

    p = Path(file_path)
    module = _module_of(p, root) if root else p.with_suffix("").as_posix().replace("/", ".")
    mod_doc = ast.get_docstring(tree) or ""
    module_docs = {module: mod_doc} if mod_doc else {}

    all_names = extract_all_names(tree)
    module_priv = module.split(".")[0].startswith("_") if module else p.stem.startswith("_")

    out: list[SymbolInfo] = []

    def add(fn: ast.FunctionDef | ast.AsyncFunctionDef, qualprefix: str, is_method: bool) -> None:
        if is_method:
            # методы наследуют публичность класса: класс должен быть публичным
            pub = not fn.name.startswith("_") and qualprefix.split(".")[-1] in visible_classes
            in_all = None
        else:
            pub = _is_public(fn.name, all_names, module_priv)
            in_all = (fn.name in all_names) if all_names is not None else None
        out.append(
            SymbolInfo(
                kind="method" if is_method else "function",
                name=fn.name,
                qualified_name=f"{qualprefix}.{fn.name}",
                file=file_path,
                line=fn.lineno,
                public=pub,
                in_all=in_all,
                signature=signature_str(fn),
                params=_params(fn),
                returns=_unparse(fn.returns),
                has_docstring=bool(ast.get_docstring(fn)),
                decorators=[_unparse(d) or "" for d in fn.decorator_list],
            )
        )

    visible_classes: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            visible_classes.add(node.name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, module, is_method=False)
        elif isinstance(node, ast.ClassDef):
            pub_cls = _is_public(node.name, all_names, module_priv)
            info = SymbolInfo(
                kind="class",
                name=node.name,
                qualified_name=f"{module}.{node.name}",
                file=file_path,
                line=node.lineno,
                public=pub_cls,
                in_all=(node.name in all_names) if all_names is not None else None,
                signature=f"class {node.name}"
                + (f"({', '.join(_unparse(b) or '' for b in node.bases)})" if node.bases else ""),
                has_docstring=bool(ast.get_docstring(node)),
                bases=[_unparse(b) or "" for b in node.bases],
                decorators=[_unparse(d) or "" for d in node.decorator_list],
            )
            out.append(info)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, f"{module}.{node.name}", is_method=True)

    return out, module_docs, errors
