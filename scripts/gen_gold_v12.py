#!/usr/bin/env python3
"""Генерация кейсов gold dataset v1.2 (gold-045..gold-054).

Новые uncovered-категории, согласованные как «Шаг A»:
  - api_removed: удаление публичного API / deprecated-сигналы (045, 046)
  - framework_migration: миграция фреймворка/CLI (047)
  - security: изменение auth-поведения (048)
  - perf: backpressure/буферизация — видимое пользователю поведение (049)
  - breaking+config: обязательная настройка без дефолта (050)
  - negative: внутренний рефакторинг с pubapi-шумом (051), чистый тест-only (052)
  - bootstrap Stage 0: repo без docs/ (053)
  - mixed PR: код + человек уже обновил доки (054)

Все диффы синтетические, на базе fixtures/demopkg.
"""
from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path("data/gold/cases")
ANNOT_DATE = "2026-10-06"


def case(
    cid: str,
    category: str,
    behavior: str,
    triggers: list[str],
    severity: str,
    files: list[str],
    summary: str,
    diff: str,
    public_api_impact: str,
    docs_change: dict | None,
    lang: str,
    labels: list[str],
    notes: str,
    donor: str,
    reference: str,
) -> dict:
    return {
        "schema_version": "gold-v1",
        "id": cid,
        "category": category,
        "expected_behavior": behavior,
        "trigger_kind": triggers,
        "severity": severity,
        "source": {
            "kind": "synthetic",
            "repo": "demo-repo",
            "pr_number": None,
            "pr_url": None,
            "donor_reference": donor,
            "reference": reference,
        },
        "code_change": {
            "files": files,
            "summary": summary,
            "diff": diff,
            "public_api_impact": public_api_impact,
        },
        "docs_change": docs_change,
        "lang": lang,
        "labels": labels,
        "annotator": {"by": "team", "reviewed_by": None, "date": ANNOT_DATE},
        "notes": notes,
        "raw_patch": None,
    }


DOCS_TARGETS = {
    "api-reference": ["docs/api-reference.md"],
    "readme": ["README.md"],
    "guide": ["docs/guide/inference.md"],
    "changelog": ["docs/changelog.md"],
    "adr": ["docs/adr/ADR-0002-storage.md"],
}


NO_DOCS = {
    "change_type": "none",
    "target_files": [],
    "target_section": None,
    "human_written_text": None,
    "must_include": [],
    "must_not_include": [],
}


def docs_add(target: str, section: str, text: str, must: list[str], must_not: list[str]) -> dict:
    return {
        "change_type": "add",
        "target_files": DOCS_TARGETS[target],
        "target_section": section,
        "human_written_text": text,
        "must_include": must,
        "must_not_include": must_not,
    }


def docs_update(target: str, section: str, text: str, must: list[str], must_not: list[str]) -> dict:
    d = docs_add(target, section, text, must, must_not)
    d["change_type"] = "update"
    return d


CASES: list[dict] = []

# ------------------------------------------------------------------ positives

CASES.append(case(
    "gold-045", "feature_doc", "write_docs", ["new_user_feature", "api_new"], "high",
    ["demopkg/auth.py"],
    "Добавлен класс APITokenAuth — аутентификация по Bearer-токенам.",
    '''--- /dev/null
+++ b/demopkg/auth.py
@@ -0,0 +1,22 @@
+"""Токенная аутентификация для DemoClient."""
+from __future__ import annotations
+
+import os
+
+
+class APITokenAuth:
+    """Аутентификация по статическому Bearer-токену.
+
+    Args:
+        token: Bearer-токен. Если не передан, читается из env DEMO_API_TOKEN.
+    """
+
+    def __init__(self, token: str | None = None):
+        self._token = token or os.environ.get("DEMO_API_TOKEN", "")
+
+    def headers(self) -> dict[str, str]:
+        return {"Authorization": f"Bearer {self._token}"}
+
+
+__all__ = ["APITokenAuth"]
''',
    "added",
    docs_add(
        "api-reference", "Auth / APITokenAuth",
        '''#### class APITokenAuth(token=None)

Аутентификация `DemoClient` по статическому Bearer-токену.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `token` | `str` или `None` | `None` | Bearer-токен; при `None` берётся из переменной окружения `DEMO_API_TOKEN`. |

Метод `headers()` возвращает заголовок `Authorization: Bearer <token>`.

```python
from demopkg.auth import APITokenAuth
auth = APITokenAuth()  # токен из DEMO_API_TOKEN
```
''',
        ["APITokenAuth", "Bearer", "DEMO_API_TOKEN", "headers"],
        ["OAuth2", "refresh_token", "APIKeyAuth", "JWT"],
    ),
    "ru", ["positive", "edge"],
    "Новый auth-механизм: частая галлюцинация малых моделей — подмешивание OAuth2/JWT вместо фактического API.",
    "https://github.com/tensorflow/docs (PR #128355: добавление новых примеров)",
    "Новый механизм аутентификации в справочнике API",
))

CASES.append(case(
    "gold-046", "api_change", "write_docs", ["api_removed", "breaking_change"], "high",
    ["demopkg/exporters.py"],
    "Удалена публичная функция export_to_orc(); в __all__ её больше нет.",
    '''--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -30,15 +30,6 @@ def export_to_parquet(df, path, compression="snappy"):
     df.to_parquet(path, compression=compression)
 
 
-def export_to_orc(df, path):
-    """Сохранить DataFrame в формате ORC (deprecated с 0.4)."""
-    warnings.warn("export_to_orc is deprecated, use export_to_parquet",
-                  DeprecationWarning, stacklevel=2)
-    df.to_parquet(path)
-
-
-__all__ = ["export_to_csv", "export_to_parquet", "export_to_orc"]
+__all__ = ["export_to_csv", "export_to_parquet"]
''',
    "removed",
    docs_update(
        "api-reference", "Exporters / export_to_orc (removed)",
        '''~~export_to_orc~~ — **удалено в версии 0.6.**

Функция выгрузки в ORC удалена из публичного API. Используйте
`export_to_parquet(df, path)` — Parquet покрывает тот же сценарий
колоночного хранения. Миграция:

```python
# было
export_to_orc(df, "out.orc")
# стало
export_to_parquet(df, "out.parquet")
```
''',
        ["удалено", "export_to_parquet", "0.6"],
        ["все еще доступна", "deprecated но работает", "export_to_avro"],
    ),
    "ru", ["positive", "edge"],
    "Uncovered v1.1: полное удаление публичной функции (не signature change). Документация должна быть помечена как removed, а не просто удалена молча.",
    "https://github.com/QGIS/QGIS-Documentation/pull/7887 (обновление раздела)",
    "Удаление API из reference с инструкцией миграции",
))

CASES.append(case(
    "gold-047", "readme_update", "write_docs", ["behavior_changed", "breaking_change"], "high",
    ["demopkg/cli.py", "demopkg/settings.py"],
    "CLI демо перенесён с argparse на click: меняется способ вызова и имя entrypoint.",
    '''--- a/demopkg/cli.py
+++ b/demopkg/cli.py
@@ -1,24 +1,18 @@
-import argparse
+import click
 
 
-def main():
-    parser = argparse.ArgumentParser(prog="demo")
-    parser.add_argument("--input", required=True)
-    parser.add_argument("--output", default="out.json")
-    args = parser.parse_args()
-    run(args.input, args.output)
-
-
-if __name__ == "__main__":
-    main()
+@click.command()
+@click.option("--input", "input_path", required=True, help="Путь к входному файлу")
+@click.option("--output", default="out.json", help="Путь к результату")
+def main(input_path: str, output: str) -> None:
+    """Запустить обработку данных демки."""
+    run(input_path, output)
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -18,4 +18,5 @@ dependencies = [
     "pandas>=2.0",
+    "click>=8.1",
 ]
 
 [project.scripts]
-demo = "demopkg.cli:main"
+demo = "demopkg.cli:main"
''',
    "changed",
    docs_update(
        "readme", "Использование / CLI",
        '''### CLI

Интерфейс командной строки построен на `click` (с версии 0.6):

```bash
demo --input data.csv --output result.json
```

| Опция | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `--input` | да | — | Путь к входному файлу. |
| `--output` | нет | `out.json` | Путь к результату. |
''',
        ["click", "--input", "--output"],
        ["argparse", "positional", "subcommand"],
    ),
    "ru", ["positive", "edge"],
    "Uncovered v1.1: миграция фреймворка CLI. Триггеры: dependency_added неявно (в exp-list не включали, т.к. главный сигнал — смена поведения вызова). Ожидается обновление README, не API-ref.",
    "https://github.com/QGIS/QGIS-Documentation/pull/7934 (обновление руководства)",
    "Смена CLI-фреймворка: обновление раздела использования",
))

CASES.append(case(
    "gold-048", "feature_doc", "write_docs", ["behavior_changed", "breaking_change"], "high",
    ["demopkg/client.py"],
    "DemoClient теперь бросает AuthError при пустом токене вместо анонимных запросов.",
    '''--- a/demopkg/client.py
+++ b/demopkg/client.py
@@ -22,8 +22,12 @@ class DemoClient:
     def __init__(self, base_url, token=None):
         self.base_url = base_url.rstrip("/")
-        self.token = token
+        self.token = token or os.environ.get("DEMO_API_TOKEN")
+        if not self.token:
+            raise AuthError(
+                "Token is required: pass token= or set DEMO_API_TOKEN"
+            )
 
     def get(self, path):
-        headers = {}
+        headers = {"Authorization": f"Bearer {self.token}"}
''',
    "changed",
    docs_update(
        "guide", "Аутентификация клиента",
        '''С версии 0.6 `DemoClient` **требует токен**: конструктор бросает
`AuthError`, если токен не передан явно и переменная окружения
`DEMO_API_TOKEN` не установлена. Анонимные запросы больше не поддерживаются.

```python
client = DemoClient("https://api.demo.dev", token="...")
# или
os.environ["DEMO_API_TOKEN"] = "..."
client = DemoClient("https://api.demo.dev")
```
''',
        ["AuthError", "DEMO_API_TOKEN", "требует токен"],
        ["анонимны", "опциональн", "warning"],
    ),
    "ru", ["positive", "breaking", "edge"],
    "Uncovered v1.1: security/auth-поведение без изменения сигнатур — ловится только текстовыми сигналами (raise, env-var), AST-API-diff тут молчит.",
    "https://github.com/tensorflow/docs (PR #128320: исправления в reference)",
    "Обязательная аутентификация: обновление руководства",
))

CASES.append(case(
    "gold-049", "feature_doc", "write_docs", ["behavior_changed", "new_user_feature"], "medium",
    ["demopkg/streaming.py"],
    "В StreamProcessor добавлен backpressure: max_buffer параметр, при переполнении — BlockTimeout.",
    '''--- a/demopkg/streaming.py
+++ b/demopkg/streaming.py
@@ -8,10 +8,17 @@ class StreamProcessor:
-    def __init__(self, handler):
+    def __init__(self, handler, max_buffer: int = 1024):
         self.handler = handler
+        self.max_buffer = max_buffer
         self._queue = deque()
 
     def push(self, item):
+        if len(self._queue) >= self.max_buffer:
+            raise BlockTimeout(f"buffer full ({self.max_buffer})")
         self._queue.append(item)
''',
    "added",
    docs_add(
        "api-reference", "Streaming / StreamProcessor",
        '''#### class StreamProcessor(handler, max_buffer=1024)

Пакетный обработчик потока событий с backpressure.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `handler` | `Callable` | — | Обработчик одного элемента. |
| `max_buffer` | `int` | `1024` | Максимум элементов в буфере. |

`push()` бросает `BlockTimeout`, когда буфер заполнен, — это сигнал
производителю снизить темп. Порог настраивается параметром `max_buffer`.
''',
        ["max_buffer", "1024", "BlockTimeout", "backpressure"],
        ["await", "asyncio.Queue", "drop oldest", "бессрочно блокирует"],
    ),
    "en", ["positive", "edge"],
    "Uncovered v1.1: perf/backpressure-семантика. Частая ошибка моделей — написать 'блокирует бессрочно' вместо фактического исключения. Кейс также тестирует lang=en.",
    "https://github.com/QGIS/QGIS-Documentation/pull/7912 (новый алгоритм)",
    "Backpressure semantics in streaming API reference",
))

CASES.append(case(
    "gold-050", "readme_update", "write_docs", ["config_or_env_changed", "breaking_change"], "high",
    ["demopkg/settings.py", ".env.example"],
    "RETRY_BACKOFF стал обязательным (без дефолта), добавлен обязательный SERVICE_REGION.",
    '''--- a/demopkg/settings.py
+++ b/demopkg/settings.py
@@ -10,7 +10,9 @@ class Settings(BaseSettings):
     api_timeout: int = 30
-    retry_backoff: float = 1.0
+    retry_backoff: float
+    service_region: str
+
+    model_config = SettingsConfigDict(env_file=".env")
--- a/.env.example
+++ b/.env.example
@@ -1,3 +1,5 @@
 DEMO_API_TOKEN=
 API_TIMEOUT=30
-RETRY_BACKOFF=1.0
+RETRY_BACKOFF=          # required, seconds
+SERVICE_REGION=         # required, e.g. eu-west-1
''',
    "changed",
    docs_update(
        "readme", "Конфигурация / Переменные окружения",
        '''### Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `RETRY_BACKOFF` | **да** (с 0.6) | — | Базовая задержка повторов, сек. Дефолт `1.0` удалён. |
| `SERVICE_REGION` | **да** (новая) | — | Регион сервиса, напр. `eu-west-1`. |
| `API_TIMEOUT` | нет | `30` | Таймаут запроса, сек. |

Обновите `.env` перед апгрейдом: отсутствие обязательных переменных
приведёт к ошибке валидации при старте.
''',
        ["RETRY_BACKOFF", "SERVICE_REGION", "обязательн"],
        ["по умолчанию 1.0", "опциональная"],
    ),
    "ru", ["positive", "breaking"],
    "Uncovered v1.1: конфиг-брейнинг без изменения публичных сигнатур; сигнал — .env.example + аннотации без дефолтов.",
    "https://github.com/tensorflow/docs (PR #128355)",
    "Required environment variables migration note",
))

# ------------------------------------------------------------------ negatives

CASES.append(case(
    "gold-051", "internal_refactor", "stay_silent", ["none"], "low",
    ["demopkg/pipeline.py"],
    "Извлечён приватный _apply_stage; публичные методы те же. В коде осталось слово 'public API' в комментарии.",
    '''--- a/demopkg/pipeline.py
+++ b/demopkg/pipeline.py
@@ -40,12 +40,16 @@ class Pipeline:
     def run(self, data):
-        for stage in self.stages:
-            data = stage.apply(data)
+        return self._apply_stages(data)
+
+    def _apply_stages(self, data):
+        # helper for the public API surface (run/astage)
+        for stage in self.stages:
+            data = stage.apply(data)
         return data
''',
    "none",
    NO_DOCS,
    "ru", ["negative", "edge"],
    "Uncovered negative v1.1: содержит маркер 'public API' в комментарии — проверка, что comment-only шум не вызывает R-ignore-refactor в обход анализа и не триггерит write_docs.",
    "—",
    "Internal refactor with misleading keyword in comment",
))

CASES.append(case(
    "gold-052", "ci_test_only", "stay_silent", ["none"], "low",
    ["tests/test_auth.py"],
    "Добавлены тесты на существующее поведение APITokenAuth (без изменений кода).",
    '''--- /dev/null
+++ b/tests/test_auth.py
@@ -0,0 +1,14 @@
+import pytest
+from demopkg.auth import APITokenAuth
+
+
+def test_headers_contains_bearer(monkeypatch):
+    monkeypatch.setenv("DEMO_API_TOKEN", "abc")
+    auth = APITokenAuth()
+    assert auth.headers() == {"Authorization": "Bearer abc"}
+
+
+def test_explicit_token_wins(monkeypatch):
+    monkeypatch.setenv("DEMO_API_TOKEN", "abc")
+    assert APITokenAuth("xyz").headers()["Authorization"] == "Bearer xyz"
''',
    "none",
    NO_DOCS,
    "ru", ["negative", "edge"],
    "Negative: тесты на уже задокументированное поведение — new file в tests/ не должен считаться новым feature.",
    "—",
    "Tests only for existing documented behavior",
))

# ------------------------------------------------------------------ bootstrap

CASES.append(case(
    "gold-053", "bootstrap", "escalate_bootstrap", ["none"], "high",
    ["git_log: last 1 commit", "repo state: no docs/, no README"],
    "Первый запуск: в репозитории нет ни docs/, ни README; bootstrap Stage 0 (скан структуры).",
    '''(no diff — initial repository scan)
repo tree:
  pyproject.toml
  src/pkg/__init__.py
  src/pkg/core.py
  src/pkg/utils.py
  tests/test_core.py
''',
    "none",
    {
        "change_type": "add",
        "target_files": ["docs/BOOTSTRAP_STAGE_0.md"],
        "target_section": "Stage 0: inventory of modules and public symbols",
        "human_written_text": "Stage 0 (inventory, детерминированный скан, без LLM): список модулей pkg.core, pkg.utils, публичных символов и зависимостей из pyproject.toml; план генерации README/docs на следующих стейджах.\n",
        "must_include": ["pkg.core", "pkg.utils", "inventory"],
        "must_not_include": ["markdown-текст README", "готовый API reference"],
    },
    "ru", ["positive", "edge"],
    "Uncovered v1.1: Stage 0 для пустого репозитория (без docs/ и README). Отличие от gold-040..044: там partial-docs, здесь полный ноль — different code path.",
    "—",
    "Bootstrap Stage 0 on empty-docs repo",
))

CASES.append(case(
    "gold-054", "api_change", "write_docs", ["api_new", "dependency_added"], "medium",
    ["demopkg/vector.py", "pyproject.toml", "docs/api-reference.md"],
    "Добавлен VectorSearch + зависимость numpy; человек УЖЕ обновил docs/api-reference.md в этом же PR.",
    '''--- /dev/null
+++ b/demopkg/vector.py
@@ -0,0 +1,14 @@
+import numpy as np
+
+
+def cosine_similarity(a, b):
+    """Косинусное сходство двух векторов одинаковой длины."""
+    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
+    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb)))
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -18,3 +18,4 @@ dependencies = [
     "pandas>=2.0",
+    "numpy>=1.26",
 ]
--- a/docs/api-reference.md
+++ b/docs/api-reference.md
@@ -55,3 +55,12 @@ See Exporters above.
+
+### VectorSearch
+
+#### cosine_similarity(a, b)
+
+Косинусное сходство двух векторов одинаковой длины. Возвращает float.
+Зависимость: `numpy>=1.26`.
''',
    "added",
    docs_update(
        "api-reference", "VectorSearch / cosine_similarity (cross-check)",
        '''Кросс-чек человеческой документации в том же PR: секция
`cosine_similarity(a, b)` уже содержит сигнатуру, описание и версию
numpy>=1.26 — агент должен сверить текст против кода и не дублировать секцию.
''',
        ["cosine_similarity", "numpy"],
        ["дублирование секции", "европейское расстояние"],
    ),
    "ru", ["positive", "multi_file", "edge"],
    "Uncovered v1.1: mixed PR (код + человек обновил доки). Ожидаем write_docs НЕ как создание новой секции, а как верификацию/дополнение; ключевой антипаттерн — дубль секции.",
    "https://github.com/QGIS/QGIS-Documentation/pull/7912",
    "Mixed PR: human docs already updated in same PR",
))


# gold-053 — bootstrap-скан, требует source.kind=bootstrap_scan (семантика валидатора)
for c in CASES:
    if c["id"] == "gold-053":
        c["source"]["kind"] = "bootstrap_scan"
        c["source"]["donor_reference"] = None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for c in CASES:
        path = OUT / f"{c['id']}.json"
        path.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    print(f"total new cases: {len(CASES)}")


if __name__ == "__main__":
    main()
