"""Тесты CLI github_int: то, что запускает пользователь (whoami, seed-pr).

Обе команды падали на живом прогоне:
  * `whoami` — `dict(RateLimitOverview)` даёт TypeError, объект не итерируется;
  * `seed-pr` — путь к патчу строился от `wd.parent.parent` (каталог НАД репо),
    а сам diff из gold-сета не проходил `git apply` («corrupt patch»).
Здесь проверяем исправления без сети.
"""
from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from docagent.github_int import cli as C  # noqa: E402


# ------------------------------------------------------------------- build_case_patch --
def test_build_case_patch_writes_inside_project(tmp_path=None):
    """Путь к патчу — в data/work проекта (раньше уходил выше репозитория)."""
    patch, diff, fixes = C.build_case_patch("gold-005")
    try:
        assert patch.parent == (REPO / "data" / "work").resolve() or \
            patch.parent == REPO / "data" / "work"
        assert REPO in patch.parents
        assert patch.exists()
        assert "diff --git a/demopkg/settings.py b/demopkg/settings.py" in diff
    finally:
        patch.unlink(missing_ok=True)


def test_build_case_patch_normalizes_counters():
    from docagent.github_int.gitpatch import count_mismatches

    case_file = REPO / "data" / "gold" / "cases" / "gold-005.json"
    raw = json.loads(case_file.read_text(encoding="utf-8"))["code_change"]["diff"]
    patch, diff, fixes = C.build_case_patch("gold-005")
    try:
        assert count_mismatches(diff) == [], "счётчики в патче обязаны сходиться"
        # сет уже починен, поэтому «как есть» тоже валиден: нормализация идемпотентна
        assert count_mismatches(raw) == []
        assert fixes == 0
        assert diff == raw if raw.endswith("\n") else diff == raw + "\n"
    finally:
        patch.unlink(missing_ok=True)


def test_build_case_patch_errors_are_clear():
    with pytest.raises(FileNotFoundError):
        C.build_case_patch("no-such-case")
    # кейс с пустым diff'ом: временный файл, чтобы не зависеть от содержимого сета
    tmp_case = REPO / "data" / "gold" / "cases" / "tmp-empty-diff.json"
    tmp_case.write_text(json.dumps({"id": "tmp-empty", "expected_behavior": "stay_silent",
                                    "code_change": {"diff": ""}}), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="пустой code_change.diff"):
            C.build_case_patch("tmp-empty-diff")
    finally:
        tmp_case.unlink(missing_ok=True)


# ------------------------------------------------------------------------- whoami --
class _FakeRate:
    def __init__(self):
        self.limit = 5000
        self.remaining = 4999
        self.reset = "2026-10-07T12:00:00Z"


class _FakeOverview:
    """Как RateLimitOverview в PyGithub: объект, не итерируемый и не словарь."""

    def __init__(self):
        self.core = _FakeRate()
        self.search = _FakeRate()


class _FakeApi:
    def get_rate_limit(self):
        return _FakeOverview()


class _FakeClient:
    token = "test-token"

    def __init__(self, **kw):
        self.api = _FakeApi()

    def whoami(self):
        return "docagent-bot"


def test_cmd_whoami_does_not_crash(monkeypatch, capsys):
    """Регрессия: `dict(c.api.get_rate_limit())` падал с TypeError."""
    monkeypatch.setattr(C, "GitHubClient", _FakeClient)
    rc = C.cmd_whoami(Namespace())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["login"] == "docagent-bot"
    assert out["rate_limit"]["core"]["remaining"] == 4999


def test_cmd_whoami_survives_rate_limit_failure(monkeypatch, capsys):
    class _Broken(_FakeClient):
        def __init__(self, **kw):
            class Api:
                def get_rate_limit(self):
                    raise RuntimeError("rate limit endpoint down")
            self.api = Api()

    monkeypatch.setattr(C, "GitHubClient", _Broken)
    assert C.cmd_whoami(Namespace()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["login"] == "docagent-bot"
    assert "error" in out["rate_limit"]


# ------------------------------------------------------------------------ seed-pr --
class _RepoFake:
    """Клиент-заглушка: путь seed-pr проверяем до сетевых шагов."""

    def __init__(self):
        self.prs = []

    def get_contents(self, path):
        from github import GithubException
        raise GithubException(404, {}, None)


def test_cmd_seed_pr_does_not_leak_patch_above_repo(monkeypatch, capsys):
    """Патч обязан лежать в data/work проекта, а не в каталоге над репозиторием."""
    created: list[Path] = []
    real_build = C.build_case_patch

    def spy(case_id, patch_dir=None):
        path, diff, fixes = real_build(case_id)
        created.append(path)
        return path, diff, fixes

    monkeypatch.setattr(C, "build_case_patch", spy)
    # клиент нужен только чтобы дойти до шага клона (сети нет — дальше упадёт)
    class _Client:
        token = "test-token"

        def __init__(self, **kw):
            pass

    monkeypatch.setattr(C, "GitHubClient", _Client)
    try:
        # сети нет: команда дойдёт до клона/fetch и упадёт — это ожидаемо
        rc = C.cmd_seed_pr(Namespace(repo="o/r", case="gold-005"))
    except subprocess.CalledProcessError:
        rc = -1
    assert created, f"патч не создан (rc={rc})"
    patch = created[0]
    try:
        assert patch.parent == REPO / "data" / "work"
        assert REPO in patch.parents
        assert "diff --git" in patch.read_text(encoding="utf-8")
        # в каталоге над репозиторием ничего не появилось
        above = REPO.parent / "data" / "work" / "gold-005.patch"
        assert not above.exists(), f"патч утёк выше репозитория: {above}"
    finally:
        patch.unlink(missing_ok=True)
