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
    print(json.dumps({"login": c.whoami(), "rate_limit":
                      dict(c.api.get_rate_limit())}, default=str)[:400])
    return 0


def cmd_bootstrap(args):
    c = GitHubClient()
    # bootstrap пишет напрямую в main демо-репо (git tree API, без PR) —
    # так мы не смешиваем историю DocScribe и python-demo-repo.
    res = run_bootstrap(c, args.repo, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_seed_pr(args):
    """Создать тестовый PR в демо-репо из gold-кейса (ветка строго от main демо-репо).

    Берёт code_change.diff кейса, применяет его к локальному клону demo_repo/
    (git apply) и пушит ветку test/<case> — без смешения с историей DocScribe.
    """
    import re as _re
    import subprocess

    c = GitHubClient()
    slug = args.repo
    case_file = REPO / "data" / "gold" / "cases" / f"{args.case}.json"
    if not case_file.exists():
        print(f"case not found: {case_file}", file=sys.stderr)
        return 2
    case = json.loads(case_file.read_text(encoding="utf-8"))
    diff = (case.get("code_change") or {}).get("diff", "")
    if not diff.strip():
        print(f"{args.case}: пустой code_change.diff (negative-кейс?)", file=sys.stderr)
        return 2

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
    patch = wd.parent.parent / "data" / "work" / f"{args.case}.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(diff, encoding="utf-8")
    r = subprocess.run(["git", "-C", str(wd), "apply", "--whitespace=nowarn",
                        str(patch)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"git apply failed: {r.stderr[:500]}", file=sys.stderr)
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
    """Полный цикл графа на реальном PR (публикация — по DOCAGENT_PUBLISH_TARGET)."""
    c = GitHubClient()
    diff = c.diff_text(args.repo, args.pr)
    from docagent.orchestrator.cli import main as orch_main
    tmp = REPO / "data" / "work" / f"{args.repo.replace('/', '_')}-pr{args.pr}.diff"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(diff, encoding="utf-8")
    # pr_ref обязателен для github-publish: без него _repo_slug падает на
    # demo_repo и ветка сливается с main -> "No commits between main and ..."
    argv = ["--diff", str(tmp), "--backend", args.backend,
            "--pr-ref", f"https://github.com/{args.repo}/pull/{args.pr}"]
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

    p = sub.add_parser("whoami", help="проверка токена")
    p.set_defaults(fn=cmd_whoami)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
