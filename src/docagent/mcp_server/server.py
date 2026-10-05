"""DocAgent MCP Server (шаг 1).

Транспорт: stdio (для клиента) + опционально streamable-http (--http).
Инструменты — тонкие обёртки над service.py; вся логика детерминированная,
LLM вызывается только внутри verify_api_change и деградирует в правила.

Запуск:
    python -m docagent.mcp_server.server                # stdio
    python -m docagent.mcp_server.server --http 8765    # streamable HTTP
"""


import json
import sys


from fastmcp import FastMCP

from docagent.config import get_settings
from docagent.mcp_server import service as svc
from docagent.mcp_server.models import (
    ApiDiffResult,
    BootstrapPlan,
    DiffAnalysis,
    DocsDecision,
    FileContent,
    PullRequestContext,
    SymbolInfo,
    VerifyResult,
)
from docagent.mcp_server.verify import LLMClient, verify_change
from docagent.mcp_server.git_source import gh_api, now_iso

INSTRUCTIONS = """DocAgent tools: детерминированный анализ git-diff/репозитория для агента документации.
Порядок для PR: get_pull_request_context -> analyze_pr_diff -> should_update_docs ->
(get_public_api / diff_public_api при сомнениях) -> verify_api_change -> plan_bootstrap (если docs/ нет).
should_update_docs — единственный источник решения «писать или молчать»; он никогда не обращается к LLM."""

mcp = FastMCP(name="docagent", instructions=INSTRUCTIONS)


def _err(e: Exception) -> dict[str, str]:
    return {"error": type(e).__name__, "detail": str(e)[:500]}


@mcp.tool()
def analyze_pr_diff(
    repo_path: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    diff_text: str | None = None,
    pr_number: int | None = None,
    staged: bool = False,
) -> DiffAnalysis:
    """Разбор unified diff: статистика файлов, изменённые символы, зависимости, env, ссылки на docs.

    Способы (по приоритету): diff_text > pr_number (git fetch refs/pull/N/head) >
    base_ref..head_ref > working tree (staged опционально). repo_path — путь к локальному клону.
    """
    s = get_settings()
    try:
        text, _, _ = svc.obtain_diff(
            repo_path=repo_path, base_ref=base_ref, head_ref=head_ref,
            diff_text=diff_text, pr_number=pr_number, staged=staged, settings=s,
        )
        analysis, _parse, _recs = svc.analyze_diff(text, s)
        return analysis
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def should_update_docs(
    repo_path: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    diff_text: str | None = None,
    pr_number: int | None = None,
    staged: bool = False,
) -> DocsDecision:
    """Решение «писать документацию или молчать» по детерминированным правилам (spec §6 шага 0).

    Возвращает trigger_kinds / ignore_reasons / severity / expected_docs_targets / evidence.
    LLM НЕ используется — это pre-filter до обращения к модели. needs_llm_verification=true
    означает, что оркестратору стоит вызвать verify_api_change для уточнения.
    """
    s = get_settings()
    try:
        text, before, after = svc.obtain_diff(
            repo_path=repo_path, base_ref=base_ref, head_ref=head_ref,
            diff_text=diff_text, pr_number=pr_number, staged=staged, settings=s,
        )
        _analysis, parse, recs = svc.analyze_diff(text, s)
        decision = svc.decide(parse, recs, s)
        # полный AST-дифф по ref'ам точнее patch-эвристики: используем его, если ref разрешимы
        if repo_path and (base_ref or pr_number) and svc.gs.is_git_repo(svc._resolve_root(repo_path)):
            root = svc._resolve_root(repo_path)
            try:
                before_rev = svc.gs.revision_from_repo(root, svc._try_resolve(root, before))
                after_rev = svc.gs.revision_from_repo(root, svc._try_resolve(root, after))
            except svc.gs.GitError:
                before_rev = after_rev = None
            if before_rev is not None and after_rev is not None:
                b_syms, _ = svc.api_diff.snapshot_files(before_rev.files, root)
                a_syms, _ = svc.api_diff.snapshot_files(after_rev.files, root)
                raw = svc.api_diff.diff_snapshots(b_syms, a_syms)
                if any(x.symbol_public for x in raw):
                    decision = svc.decide(parse, raw, s)
        return decision
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def get_public_api(ref: str | None = None, repo_path: str | None = None) -> list[SymbolInfo] | dict:
    """Публичный API репозитория на указанном ref (HEAD по умолчанию).

    Публичность — эвристика: без `_`-префикса + учитываем `__all__`. Только чтение.
    """
    try:
        root = svc._resolve_root(repo_path)
        return svc.api_snapshot(root, ref)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def diff_public_api(
    before_ref: str,
    after_ref: str,
    repo_path: str | None = None,
) -> ApiDiffResult:
    """Детерминированный дифф публичного API между двумя git-ref (added/removed/changed/breaking)."""
    try:
        root = svc._resolve_root(repo_path)
        res = svc.api_diff_full(root, before_ref, after_ref)
        if res is None:
            return {"error": "UnresolvableRefs",
                    "detail": f"нельзя разрешить {before_ref}/{after_ref} в {root}"}
        return res
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def read_file(
    path: str,
    ref: str | None = None,
    repo_path: str | None = None,
) -> FileContent:
    """Содержимое файла из рабочей директории или с git-ref (только чтение, path sandboxed)."""
    try:
        root = svc._resolve_root(repo_path)
        return svc.read_file_at(root, path, ref, get_settings())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def search_codebase(
    query: str,
    repo_path: str | None = None,
    limit: int = 20,
) -> list:
    """Поиск подстроки по коду/документации (git grep или rglob-fallback)."""
    try:
        root = svc._resolve_root(repo_path)
        return svc.search_codebase(root, query, get_settings(), limit=min(limit, 100))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def plan_bootstrap(repo_path: str | None = None) -> BootstrapPlan:
    """Детерминированный bootstrap-план (5 стейджей, см. §5 плана шага 0): каркас docs/, README,
    API-ref по чанкам, ADR-скелеты, changelog. LLM не вызывается — сканер готовит структуру и чанки,
    текст потом пишет агент по одному чанку за draft PR."""
    try:
        root = svc._resolve_root(repo_path)
        return svc.bootstrap_plan(root, get_settings())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def verify_api_change(
    change_json: str,
    repo_path: str | None = None,
    before_ref: str | None = None,
    after_ref: str | None = None,
) -> VerifyResult:
    """LLM-верификация: является ли изменение публичного API видимым пользователю.

    change_json — объект ApiChange (например, элемент ответа diff_public_api).
    Можно передать вместо change_json пары before_ref/after_ref+symbol — тогда берём
    первый changed-элемент из diff_public_api. Без доступного LLM возвращается
    детерминированный rule-fallback (used_llm=false) — сервер не падает.
    """
    s = get_settings()
    try:
        if change_json:
            data = json.loads(change_json)
            from docagent.mcp_server.models import ApiChange

            change = ApiChange.model_validate(data)
        elif repo_path and before_ref and after_ref:
            root = svc._resolve_root(repo_path)
            res = svc.api_diff_full(root, before_ref, after_ref)
            if not res or not res.changes:
                return {"error": "NoChanges", "detail": "diff_public_api пуст"}
            change = res.changes[0]
        else:
            return {"error": "BadArguments", "detail": "нужен change_json либо before/after ref"}
        client = LLMClient(s) if s.use_llm else None
        return verify_change(change, s, client)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def get_pull_request_context(
    repo: str,
    pr_number: int,
    local_repo_path: str | None = None,
) -> PullRequestContext:
    """Метаданные PR: title/body/branch/sha/labels/changed files.

    Источник: GitHub REST (curl + GITHUB_TOKEN, если задан). Офлайн-fallback:
    git fetch refs/pull/N/head в локальном клоне local_repo_path. Только чтение.
    """
    try:
        if "/" not in repo:
            return {"error": "BadRepo", "detail": "repo должен быть 'owner/name'"}
        try:
            pr = gh_api(f"repos/{repo}/pulls/{pr_number}")
            files = gh_api(f"repos/{repo}/pulls/{pr_number}/files?per_page=100")
            return PullRequestContext(
                repo=repo,
                number=pr_number,
                title=pr.get("title", ""),
                body=pr.get("body") or "",
                author=(pr.get("user") or {}).get("login", ""),
                source_branch=(pr.get("head") or {}).get("ref", ""),
                target_branch=(pr.get("base") or {}).get("ref", ""),
                head_sha=(pr.get("head") or {}).get("sha", ""),
                base_sha=(pr.get("base") or {}).get("sha", ""),
                labels=[l.get("name", "") for l in pr.get("labels", [])],
                changed_files=[f.get("filename", "") for f in files],
                draft=bool(pr.get("draft")),
                merged=bool(pr.get("merged")),
                url=pr.get("html_url"),
                fetched_at=now_iso(),
            )
        except Exception as gh_err:  # noqa: BLE001
            if not local_repo_path:
                raise
            root = svc._resolve_root(local_repo_path)
            diff_text, base_sha, head_sha = svc.gs.diff_pr_branches(root, pr_number)
            _a, parse, _r = svc.analyze_diff(diff_text, get_settings())
            return PullRequestContext(
                repo=repo,
                number=pr_number,
                title=f"PR #{pr_number} (из локального клона)",
                body="",
                author="",
                source_branch="refs/pull/head",
                target_branch="base",
                head_sha=head_sha,
                base_sha=base_sha,
                changed_files=[f.path for f in parse.files],
                offline=True,
                url=None,
                fetched_at=now_iso(),
                error=str(gh_err)[:200],
            )
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Resources: конфигурация и справочник правил (без секретов)
# --------------------------------------------------------------------------- #
_CONFIG_KEYS = [
    "docs_backend", "model", "openai_base_url", "docs_lang",
    "typo_max_changed_lines", "typo_min_new_words", "max_diff_bytes",
    "max_chunk_lines", "trigger_kinds_enabled",
]


@mcp.resource("config://docagent")
def config_resource() -> str:
    """Активная конфигурация DocAgent (секреты исключены)."""
    s = get_settings().model_dump(mode="json")
    safe = {k: s[k] for k in _CONFIG_KEYS if k in s}
    return json.dumps(safe, ensure_ascii=False, indent=2)


_RULES_MD = """# Правила триггеров (spec §6 шага 0)

## IGNORE (агент молчит)
| Правило | Условие |
|---|---|
| R-ignore-ci-test | PR трогает только CI/тесты |
| R-ignore-docs-only | Изменена только документация |
| R-ignore-typo | ≤ typo_max_changed_lines строк и < typo_min_new_words новых слов (орфография/пунктуация) |
| R-ignore-style | Множество токенов кода не изменилось (reformat/lint/reorder) |
| R-ignore-refactor | Тело менялось, но публичный API и сигнатуры идентичны |
| R-default-silent | Ни одно триггер-правило не сработало |

## TRIGGER (агент пишет)
| Триггер | Условие | Куда пишем |
|---|---|---|
| api_new | новый публичный def/class | api-reference |
| api_signature_changed | параметры/дефолты/аннотации изменились | api-reference (+verify) |
| api_removed | публичный символ удалён | api-reference, changelog |
| behavior_changed | тело публичной функции менялся без смены сигнатуры | guide (needs_llm) |
| breaking_change | удалён параметр/обязательный параметр/дефолт | везде |
| dependency_added | новая зависимость в pyproject/requirements | readme |
| config_or_env_changed | новые env-переменные/конфиги | readme |
| adr_new / adr_changed | файлы docs/adr/* или раздел с решением | adr |

Все пороги настраиваются через config://docagent.
"""


@mcp.resource("rules://triggers")
def rules_resource() -> str:
    """Справочник правил should_update_docs (markdown)."""
    return _RULES_MD


@mcp.prompt()
def review_pr(diff_text: str, lang: str = "ru") -> str:
    """Готовый промпт для LLM-клиента: решить по анализу, нужна ли документация."""
    return (
        f"Ты — агент документации. Языковой режим: {lang}.\n"
        f"1) Вызови should_update_docs с этим diff.\n"
        f"2) Если should_update=true — составь план правки по expected_docs_targets.\n"
        f"3) Если needs_llm_verification=true — уточни через verify_api_change.\n\n"
        f"DIFF:\n{diff_text[:6000]}"
    )


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if "--http" in argv:
        i = argv.index("--http")
        port = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 8765
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
