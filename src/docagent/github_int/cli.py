"""CLI GitHub-интеграции (Шаг 4).

Примеры:
  # наполнить демо-репозиторий кодом (идемпотентно; сначала --dry-run!)
  python -m docagent.github_int.cli bootstrap --repo Reactivity512/python-demo-repo --dry-run
  python -m docagent.github_int.cli bootstrap --repo Reactivity512/python-demo-repo

  # гибрид: diff+клон реального PR, анализ правилами шага 1 (без LLM)
  python -m docagent.github_int.cli diff --repo owner/name --pr 7

  # e2e на реальном PR: полный граф с публикацией draft PR в демо-репо
  GITHUB_TOKEN=... DOCAGENT_PUBLISH_TARGET=github \\
  python -m docagent.github_int.cli run --repo owner/name --pr 7 --backend ollama --auto-approve
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from docagent.config import get_settings  # noqa: E402
from docagent.github_int.bootstrap import run_bootstrap  # noqa: E402
from docagent.github_int.client import GitHubClient  # noqa: E402


def cmd_whoami(args):
    c = GitHubClient()
    out = {"login": c.whoami()}
    # get_rate_limit() возвращает объект RateLimitOverview, а не словарь:
    # dict(obj) падает с TypeError, поэтому берём поля явно
    try:
        rl = c.api.get_rate_limit()
        core = getattr(rl, "core", None)
        search = getattr(rl, "search", None)
        out["rate_limit"] = {
            "core": {"limit": getattr(core, "limit", None),
                     "remaining": getattr(core, "remaining", None),
                     "reset": str(getattr(core, "reset", ""))},
            "search": {"limit": getattr(search, "limit", None),
                       "remaining": getattr(search, "remaining", None)},
        }
    except Exception as e:  # noqa: BLE001 — лимиты вторичны, логин важнее
        out["rate_limit"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_bootstrap(args):
    c = GitHubClient()
    # bootstrap пишет напрямую в main демо-репо (git tree API, без PR) —
    # так мы не смешиваем историю DocScribe и python-demo-repo.
    res = run_bootstrap(c, args.repo, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def build_case_patch(case_id: str, patch_dir: Path | None = None) -> tuple[Path, str, int]:
    """Готовит патч кейса для `git apply`. Возвращает (путь, текст, число правок).

    Вынесено из `cmd_seed_pr`, чтобы путь и нормализацию можно было проверить без
    сети: раньше путь строился как `wd.parent.parent`, то есть каталог НАД
    репозиторием, и там же git apply получал «corrupt patch» (файла-то нет).
    """
    from docagent.github_int.gitpatch import count_mismatches, normalize_patch

    case_file = REPO / "data" / "gold" / "cases" / f"{case_id}.json"
    if not case_file.exists():
        raise FileNotFoundError(f"case not found: {case_file}")
    case = json.loads(case_file.read_text(encoding="utf-8"))
    raw_diff = (case.get("code_change") or {}).get("diff", "")
    if not raw_diff.strip():
        raise ValueError(f"{case_id}: пустой code_change.diff (negative-кейс?)")
    problems = count_mismatches(raw_diff)
    diff = normalize_patch(raw_diff)
    out_dir = Path(patch_dir) if patch_dir else REPO / "data" / "work"
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / f"{case_id}.patch"
    patch.write_text(diff, encoding="utf-8")
    return patch, diff, len(problems)


def cmd_seed_pr(args):
    """Создать тестовый PR в демо-репо из gold-кейса (ветка строго от main демо-репо).

    Берёт code_change.diff кейса, применяет его к локальному клону demo_repo/
    (git apply) и пушит ветку test/<case> — без смешения с историей DocScribe.
    Diff проходит через `gitpatch.normalize_patch`: в gold-сете нет заголовков
    `diff --git`, а счётчики hunk'ов не совпадают с телом, поэтому «как есть»
    git apply падает с «corrupt patch».
    """
    import subprocess

    c = GitHubClient()
    slug = args.repo
    try:
        patch, _diff, fixes = build_case_patch(args.case)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if fixes:
        print(f"{args.case}: нормализовал diff (расхождений счётчиков: {fixes}) — "
              "иначе git apply не примет", file=sys.stderr)

    case_file = REPO / "data" / "gold" / "cases" / f"{args.case}.json"
    case = json.loads(case_file.read_text(encoding="utf-8"))

    wd = REPO / "demo_repo"  # локальный клон ЦЕЛЕВОГО демо-репо (пункт 1 ритуала)
    token_url = f"https://x-access-token:{c.token}@github.com/{slug}.git"
    if not (wd / ".git").exists():
        subprocess.run(["git", "clone", token_url, str(wd)], check=True,
                       capture_output=True)
    branch = f"test/{args.case}"
    for cmd in (["git", "-C", str(wd), "fetch", "origin", "main"],
                ["git", "-C", str(wd), "checkout", "-B", branch, "origin/main"],
                ["git", "-C", str(wd), "clean", "-fd"]):
        subprocess.run(cmd, check=True, capture_output=True)
    r = subprocess.run(["git", "-C", str(wd), "apply", "--whitespace=nowarn",
                        str(patch)], capture_output=True, text=True)
    if r.returncode != 0:
        # вторая попытка: просим git пересчитать счётчики сам (на случай diff'а
        # из внешнего источника, который не проходил через normalize_patch)
        r2 = subprocess.run(["git", "-C", str(wd), "apply", "--recount",
                             "--whitespace=nowarn", str(patch)],
                            capture_output=True, text=True)
        if r2.returncode != 0:
            print(f"git apply failed: {r.stderr.strip()[:300]}", file=sys.stderr)
            print(f"git apply --recount failed: {r2.stderr.strip()[:300]}",
                  file=sys.stderr)
            print(f"patch: {patch}", file=sys.stderr)
            return 3
    for cmd in (["git", "-C", str(wd), "add", "-A"],
                ["git", "-C", str(wd), "-c", "user.name=docagent-bot",
                 "-c", "user.email=bot@local", "commit", "-m",
                 f"test: seed {args.case} ({case.get('category', '')})"],
                ["git", "-C", str(wd), "push", "--force", token_url,
                 f"HEAD:refs/heads/{branch}"]):
        subprocess.run(cmd, check=True, capture_output=True)

    title = f"Test PR: {args.case} — {(case.get('code_change') or {}).get('summary', '')[:60]}"
    body = (f"seeded from gold/{args.case}\n\n"
            f"category: {case.get('category')}\n"
            f"expected_behavior: {(case.get('expected_behavior') or {}).get('behavior') if isinstance(case.get('expected_behavior'), dict) else case.get('expected_behavior')}")
    try:
        pr = c.open_draft_pr(slug, branch, title, body)
    except Exception as e:
        m = _re.search(r"already exists.*?#(\d+)", str(e))
        pr = {"number": int(m.group(1)), "html_url": f"(existing #{m.group(1)})"} \
            if m else {"error": str(e)[:200]}
    print(json.dumps({"branch": branch, **pr}, ensure_ascii=False, indent=2))
    return 0


def cmd_watch(args):
    """Шаг 5: polling-цикл ревью — комментарии PR -> правка/пояснение в том же PR.

    Требует уже остановленный на human_approval thread (его создал `run`).
    Мержит PR лид руками; после мержа watcher закрывает тред сам.
    """
    from docagent.config import get_settings
    from docagent.github_int.client import GitHubClient
    from docagent.github_int.refs import parse_pr_ref
    from docagent.github_int.watcher import env_interval, process_once, watch
    from docagent.orchestrator.graph import compile_pipeline

    s = get_settings()
    graph, cleanup = compile_pipeline()
    client = GitHubClient(settings=s)
    tid = args.thread
    branch = f"{s.bot_branch_prefix}{tid}"
    cfg = {"configurable": {"thread_id": tid}}
    snap = graph.get_state(cfg)
    if not (snap and snap.values):
        cleanup()
        print(json.dumps({"error": f"нет состояния графа для thread={tid!r}: "
                                   "сначала запусти 'run' на PR", "thread": tid},
                         ensure_ascii=False, indent=2))
        return 2
    slug = args.repo or ""
    number = args.pr
    if not slug or not number:
        st = snap.values
        slug = slug or st.get("pr_slug") or ""
        number = number or st.get("pr_number")
        if not slug or not number:
            slug2, num2 = parse_pr_ref(st.get("pr_ref", ""))
            slug, number = slug or slug2, number or num2
    if not slug or not number:
        cleanup()
        print(json.dumps({"error": "не определил repo/PR: передай --repo и --pr "
                                   "или запусти run с --pr-ref", "thread": tid},
                         ensure_ascii=False, indent=2))
        return 2

    try:
        if args.once:
            res = process_once(thread_id=tid, branch=branch, graph=graph,
                               client=client, s=s, pr_ref=f"{slug}#{number}",
                               dry_run=args.dry_run)
        else:
            res = watch(thread_id=tid, branch=branch, graph=graph, client=client,
                        s=s, pr_ref=f"{slug}#{number}",
                        interval_s=args.interval or env_interval(),
                        dry_run=args.dry_run)
    finally:
        cleanup()
    print(json.dumps({"thread_id": tid, "branch": branch, "repo": slug,
                      "pr": number, "done": res.done, "reason": res.reason,
                      "actions": res.actions}, ensure_ascii=False, indent=2))
    return 0


def cmd_eval(args):
    """Шаг 6: gold-регрессия (гейт) и/или онлайн-метрики приёмки правок."""
    from docagent.eval import gold as G
    from docagent.eval import online as O

    out_dir = Path(args.report_dir) if args.report_dir else REPO / "data" / "reports"
    out = {}
    rc = 0
    if args.what in ("gold", "all"):
        rep = G.evaluate(backend=args.backend, limit=args.limit)
        js, md = G.save(rep, out_dir)
        out["gold"] = {"passed": rep.passed, "checks": rep.checks,
                       "report_json": str(js), "report_md": str(md),
                       "errors": rep.errors}
        rc = 0 if rep.passed else 1
    if args.what in ("online", "all"):
        m = O.collect()
        js, md = O.save(m, out_dir)
        out["online"] = {"totals": m.totals, "report_json": str(js),
                         "report_md": str(md)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


def cmd_diff(args):
    """Гибридный read-side: REST-diff + shallow-клон head-рефа."""
    c = GitHubClient()
    diff = c.diff_text(args.repo, args.pr)
    files = c.changed_files(args.repo, args.pr)
    out = {"pr": f"{args.repo}#{args.pr}", "diff_chars": len(diff),
           "files": [f["filename"] for f in files]}
    if not args.no_clone:
        out["clone_path"] = str(c.clone_head(args.repo, args.pr))
    from docagent.mcp_server.service import decide_on_diff
    decision = decide_on_diff(diff, get_settings())
    out["behavior"] = decision.behavior.value
    out["targets"] = [t.value for t in decision.expected_docs_targets]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args):
    """Полный цикл графа на реальном PR (публикация — по DOCAGENT_PUBLISH_TARGET).

    Diff и метаданные PR тянет нода fetch_pr через GitHubClient (шаг 4), поэтому
    ссылка передаётся в граф целиком: '--pr-ref' обязателен для github-публикации,
    иначе publish ушёл бы в DEMO_REPO и GitHub вернул бы
    'No commits between main and <branch>'.
    """
    from docagent.orchestrator.cli import main as orch_main

    pr_url = f"https://github.com/{args.repo}/pull/{args.pr}"
    argv = ["--pr-ref", pr_url, "--backend", args.backend]
    if args.auto_approve:
        argv.append("--auto-approve")
    return orch_main(argv)


def main(argv=None):
    ap = argparse.ArgumentParser(description="DocAgent GitHub integration (step 4)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="наполнить демо-репо пакетом demopkg")
    p.add_argument("--repo", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_bootstrap)

    p = sub.add_parser("seed-pr", help="тестовый PR из gold-кейса в демо-репо")
    p.add_argument("--repo", default="Reactivity512/python-demo-repo")
    p.add_argument("--case", required=True, help="id кейса, напр. gold-045")
    p.set_defaults(fn=cmd_seed_pr)

    p = sub.add_parser("diff", help="гибридный read: diff+клон+анализ правил")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--no-clone", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("run", help="полный цикл графа на реальном PR")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--backend", default="fake", choices=["fake", "ollama", "vllm"])
    p.add_argument("--auto-approve", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("watch", help="HITL: опрашивать комментарии PR и править/отвечать")
    p.add_argument("--thread", required=True, help="thread_id из 'run' (он же ветка docagent/<thread>)")
    p.add_argument("--repo", default=None, help="owner/name (по умолчанию — из состояния)")
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--interval", type=float, default=None, help="секунды между опросами")
    p.add_argument("--once", action="store_true", help="один проход без ожидания")
    p.add_argument("--dry-run", action="store_true", help="только показать, что сделал бы")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("whoami", help="проверка токена")
    p.set_defaults(fn=cmd_whoami)

    p = sub.add_parser("eval", help="шаг 6: gold-регрессия и онлайн-метрики приёмки")
    p.add_argument("--what", default="all", choices=["gold", "online", "all"])
    p.add_argument("--backend", default="fake", choices=["fake", "ollama", "vllm"])
    p.add_argument("--limit", type=int, default=None, help="ограничить число кейсов")
    p.add_argument("--report-dir", default=None)
    p.set_defaults(fn=cmd_eval)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
