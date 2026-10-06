"""Bootstrap демо-репозитория (шаг 4): наполнение Reactivity512/python-demo-repo.

Создаёт на main коммит с минимальным Python-пакетом `demopkg` + README, чтобы
было что анализировать в e2e. Идемпотентен: файлы, которые уже есть в репо,
не перезаписывает (сверка по SHA blob'ов).

Использование:
  python -m docagent.github_int.cli bootstrap --repo owner/name [--dry-run]
"""
from __future__ import annotations

import base64

from github import GithubException

from ..config import Settings
from .client import GitHubClient

# --- содержимое демо-пакета -----------------------------------------------------
FILES: dict[str, str] = {
    "README.md": """\
# python-demo-repo

Демо-репозиторий для DocScribe (агент автодокументации с HITL).

## Установка

```bash
pip install -e .
```

## Быстрый старт

```python
from demopkg import Client

client = Client(token="demo-token")
items = client.list_items(page_size=10)
print(items.total)
```
""",
    "pyproject.toml": """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "demopkg"
version = "0.1.0"
description = "Demo package for DocScribe agent"
requires-python = ">=3.10"
dependencies = [
    "requests>=2.31",
]

[tool.setuptools.packages.find]
include = ["demopkg*"]
""",
    "demopkg/__init__.py": '''\
"""demopkg — минимальный демо-пакет для тестов DocScribe."""

from .client import Client
from .errors import ApiError, RateLimitError

__all__ = ["Client", "ApiError", "RateLimitError"]
__version__ = "0.1.0"
''',
    "demopkg/client.py": '''\
"""HTTP-клиент демо-API."""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .errors import ApiError, RateLimitError

DEFAULT_TIMEOUT = 30


@dataclass
class Page:
    """Страница результатов list_items()."""

    items: list[dict] = field(default_factory=list)
    total: int = 0


class Client:
    """Клиент демо-API.

    :param token: API-токен (заголовок Authorization).
    :param base_url: базовый URL API.
    """

    def __init__(self, token: str, base_url: str = "https://api.demo.dev/v1",
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        if not token:
            raise ValueError("token is required")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_items(self, page_size: int = 20, cursor: str | None = None) -> Page:
        """Постраничный список элементов.

        :param page_size: размер страницы (1..100).
        :param cursor: курсор следующей страницы из предыдущего ответа.
        """
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be in 1..100")
        params = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        resp = self._session.get(f"{self.base_url}/items", params=params,
                                 timeout=self.timeout)
        self._raise_for_status(resp)
        data = resp.json()
        return Page(items=data.get("items", []), total=data.get("total", 0))

    def get_item(self, item_id: str) -> dict:
        """Получить элемент по id; KeyError->ApiError при 404."""
        resp = self._session.get(f"{self.base_url}/items/{item_id}",
                                 timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code == 429:
            raise RateLimitError(retry_after=int(
                resp.headers.get("Retry-After", "60")))
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, resp.text[:200])
''',
    "demopkg/errors.py": '''\
"""Исключения demopkg."""


class DemopkgError(Exception):
    """Базовое исключение пакета."""


class ApiError(DemopkgError):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class RateLimitError(ApiError):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(429, f"retry after {retry_after}s")
        self.retry_after = retry_after
''',
    ".gitignore": """\
__pycache__/
*.pyc
*.egg-info/
dist/
build/
.env
.venv/
""",
}


def encode(content: str) -> str:
    return base64.b64encode(content.encode()).decode()


def plan_bootstrap(client: GitHubClient, slug: str) -> list[dict]:
    """Файлы, которых ещё нет в репо (идемпотентный план)."""
    repo = client.repo(slug)
    plan = []
    for path, content in FILES.items():
        try:
            repo.get_content(path)
            continue  # уже есть — не трогаем
        except GithubException as e:
            if e.status != 404:
                raise
        plan.append({"path": path, "content": encode(content), "b64": True,
                     "_plain": content})
    return plan


def run_bootstrap(client: GitHubClient, slug: str, dry_run: bool = False,
                  branch: str = "main") -> dict:
    plan = plan_bootstrap(client, slug)
    if not plan:
        return {"created": 0, "skipped": len(FILES), "commit": None}
    if dry_run:
        return {"created": len(plan), "skipped": len(FILES) - len(plan),
                "paths": [p["path"] for p in plan], "commit": "DRY-RUN"}
    head_sha = client.repo(slug).get_branch(branch).commit.sha
    sha = client.create_blob_commit(slug, branch, head_sha, plan,
                                    "chore: bootstrap demo package demopkg")
    return {"created": len(plan), "skipped": len(FILES) - len(plan),
            "paths": [p["path"] for p in plan], "commit": sha}
