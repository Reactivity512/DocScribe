#!/usr/bin/env python3
"""Генератор gold-датасета v1 (шаг 0).

Все кейсы — synthetic: смоделированы по мотивам реальных PR из открытых
донорских репозиториев (tensorflow/docs, QGIS/QGIS-Documentation), но живут в
нашей собственной фиктивной Python-библиотеке `demopkg`. Это даёт:
  * воспроизводимость и право на любое использование (нет зависимости от API GitHub);
  * возможность позже создать эти же PR в своём демо-репозитории и прогнать живой HITL-цикл;
  * контроль баланса категорий (>=30% negative) и наличие "ловушек" для малой модели.

Данные внутри кейсов консистентны: имена функций/параметров из must_include
реально присутствуют в diff, а human_written_text ссылается только на них.

Использование:
    python scripts/build_gold_dataset.py [--out data/gold/cases] [--force]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = "demo-repo"            # наш будущий публичный Python-демо-репозиторий
DONOR_TF = "https://github.com/tensorflow/docs"
DONOR_QGIS = "https://github.com/QGIS/QGIS-Documentation"


def case(
    cid: str,
    category: str,
    behavior: str,
    trigger: list[str],
    files: list[str],
    summary: str,
    diff: str,
    change_type: str,
    target_files: list[str],
    target_section: str | None,
    text: str | None,
    must_include: list[str],
    must_not_include: list[str],
    lang: str,
    labels: list[str],
    severity: str,
    reference: str,
    donor: str | None,
    notes: str,
    api_impact: str | None = None,
) -> dict:
    code_change = {"files": files, "summary": summary, "diff": diff}
    if api_impact:
        code_change["public_api_impact"] = api_impact
    docs_change = {
        "change_type": change_type,
        "target_files": target_files,
        "target_section": target_section,
        "human_written_text": text,
        "must_include": must_include,
        "must_not_include": must_not_include,
    }
    return {
        "schema_version": "gold-v1",
        "id": cid,
        "category": category,
        "expected_behavior": behavior,
        "trigger_kind": trigger,
        "severity": severity,
        "source": {
            "kind": "bootstrap_scan" if behavior == "escalate_bootstrap" else "synthetic",
            "repo": REPO,
            "pr_number": None,
            "pr_url": None,
            "donor_reference": donor,
            "reference": reference,
        },
        "code_change": code_change,
        "docs_change": docs_change,
        "lang": lang,
        "labels": labels,
        "annotator": {"by": "team", "reviewed_by": None, "date": "2026-10-05"},
        "notes": notes,
        "raw_patch": None,
    }


# ---------------------------------------------------------------- positive: api_change
API_NEW_RU = case(
    "gold-001", "api_change", "write_docs", ["api_new"],
    ["demopkg/exporters.py"],
    "Добавлена публичная функция export_to_parquet() с параметром compression.",
    """--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -12,6 +12,18 @@ def export_to_csv(df, path, sep=","):
     df.to_csv(path, sep=sep)
 
 
+def export_to_parquet(df, path, compression="snappy"):
+    \"\"\"Сохранить DataFrame в формате Parquet.
+
+    Args:
+        df: данные для выгрузки.
+        path: целевой файл.
+        compression: алгоритм сжатия, по умолчанию "snappy".
+    \"\"\"
+    df.to_parquet(path, compression=compression)
+
+
 __all__ = ["export_to_csv", "export_to_parquet"]
""",
    "add", ["docs/api-reference.md"], "Exporters / export_to_parquet",
    """#### export_to_parquet(df, path, compression="snappy")

Выгружает DataFrame в файл формата Parquet.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `df` | `pandas.DataFrame` | — | Данные для выгрузки. |
| `path` | `str` | — | Путь к целевому `.parquet` файлу. |
| `compression` | `str` | `"snappy"` | Алгоритм сжатия: `snappy`, `gzip` или `zstd`. |

Возвращает `None`. Формат Parquet предпочтителен для больших датасетов:
файл компакнее CSV и читается без потери типов столбцов.
""",
    ["export_to_parquet", "compression", "snappy", "Parquet"],
    ["export_to_orc", "lz4", "row_group_size"],
    "ru", ["positive"], "high",
    "Новый экспортёр данных в API reference",
    DONOR_QGIS + "/pull/7912 (документация нового алгоритма обработки)",
    "Эталон секции API-ref: сигнатура, таблица параметров, значение по умолчанию. "
    "must_not_include содержит частые галлюцинации qwen2.5-coder:3b (несуществующие форматы/параметры).",
    api_impact="added",
)

API_CHANGED_EN = case(
    "gold-002", "api_change", "write_docs", ["api_signature_changed", "breaking_change"],
    ["demopkg/client.py"],
    "У fetch_records() изменён порядок аргументов и добавлен обязательный timeout; обратно несовместимо.",
    """--- a/demopkg/client.py
+++ b/demopkg/client.py
@@ -31,7 +31,7 @@ class APIClient:
-    def fetch_records(self, endpoint, limit=100):
+    def fetch_records(self, endpoint, timeout, limit=100):
         \"""Fetch records from the given endpoint.\"""
         url = f"{self.base_url}/{endpoint}"
-        resp = self.session.get(url, params={"limit": limit})
+        resp = self.session.get(url, params={"limit": limit}, timeout=timeout)
         resp.raise_for_status()
         return resp.json()["items"]
""",
    "update", ["docs/api-reference.md"], "APIClient.fetch_records",
    """#### APIClient.fetch_records(endpoint, timeout, limit=100)

Retrieves a page of records from the API.

> **Breaking change (v0.4):** the `timeout` argument is now required and
> positioned before `limit`. Calls written as `fetch_records("users", 50)`
> must be updated to `fetch_records("users", 5.0, limit=50)`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint` | `str` | — | Resource path, e.g. `"users"`. |
| `timeout` | `float` | — | Seconds to wait for a response. No default. |
| `limit` | `int` | `100` | Maximum number of records per page. |

Raises `requests.Timeout` when the deadline is exceeded.
""",
    ["timeout", "Breaking change", "fetch_records", "limit"],
    ["max_retries", "backoff"],
    "en", ["positive", "breaking"], "high",
    "Изменение сигнатуры публичного метода + breaking change",
    DONOR_TF + "/pull/128320 (правка API reference)",
    "Проверяем, что агент явно помечает ломающее изменение и показывает миграцию вызова. "
    "Ключевой кейс для recall метрики Trigger.",
    api_impact="changed",
)

API_REMOVED_RU = case(
    "gold-003", "api_change", "write_docs", ["api_removed", "breaking_change"],
    ["demopkg/utils/deprecated.py", "demopkg/__init__.py"],
    "Удалена публичная функция normalize_strings(), ранее публиковавшаяся через demopkg.__all__.",
    """--- a/demopkg/__init__.py
+++ b/demopkg/__init__.py
@@ -4,7 +4,6 @@ from .client import APIClient
 from .exporters import export_to_csv
-from .utils.deprecated import normalize_strings
 
-__all__ = ["APIClient", "export_to_csv", "normalize_strings"]
+__all__ = ["APIClient", "export_to_csv"]
--- a/demopkg/utils/deprecated.py
+++ /dev/null
@@ -1,9 +0,0 @@
-def normalize_strings(df, columns, lower=True):
-    \"\"\"Deprecated: use demopkg.text.clean instead.\"\"\"
-    ...
""",
    "update", ["docs/api-reference.md", "docs/migration.md"], "Удалённые символы",
    """### Удалённые символы

Функция `normalize_strings()` удалена из публичного API (`demopkg.__all__`).

Замена — `demopkg.text.clean(df, columns, lower=True)`. Отличие: `clean`
не изменяет DataFrame на месте, а возвращает копию, поэтому код вида
`normalize_strings(df, ["name"])` нужно переписать как
`df = clean(df, ["name"])`.

Поддержка старого имени прекращена, алиасы не сохраняются.
""",
    ["normalize_strings", "demopkg.text.clean", "возвращает копию"],
    ["deprecated до v1.0", "aliased"],
    "ru", ["positive", "multi_file", "breaking"], "medium",
    "Удаление публичной функции: правка API-ref + заметка в migration guide",
    None,
    "Тест на два файла документации одновременно и на указание замены, а не просто 'удалено'.",
    api_impact="removed",
)

API_DEFAULT_CHANGED_EN = case(
    "gold-004", "api_change", "write_docs", ["api_signature_changed", "behavior_changed"],
    ["demopkg/pipeline.py"],
    "Значение по умолчанию у retries изменено с 3 на 5 — поведение изменилось без смены сигнатуры.",
    """--- a/demopkg/pipeline.py
+++ b/demopkg/pipeline.py
@@ -58,7 +58,7 @@ class RetryPolicy:
-    def __init__(self, retries=3, backoff=1.5):
+    def __init__(self, retries=5, backoff=1.5):
         self.retries = retries
         self.backoff = backoff
""",
    "update", ["docs/api-reference.md"], "RetryPolicy",
    """### RetryPolicy(retries=5, backoff=1.5)

Controls automatic retries for transient failures.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `retries` | `int` | `5` | Number of retry attempts. Changed from `3` in v0.4. |
| `backoff` | `float` | `1.5` | Exponential backoff multiplier between attempts. |

With the new default, a failing request is attempted up to six times in
total (one initial attempt plus five retries).
""",
    ["retries", "5", "backoff", "1.5"],
    ["retry_after", "jitter"],
    "en", ["positive", "edge"], "medium",
    "Смена значения по умолчанию = изменение поведения (частый ложно-негативный случай)",
    None,
    "Хитрый кейс: diff минимальный, имя символа не менялся — префильтр по 'новым def/class' его не поймает. "
    "Проверяем правило behavior_changed.",
    api_impact="changed",
)

API_MULTI_FILE_RU = case(
    "gold-005", "api_change", "write_docs", ["api_new", "dependency_added"],
    ["demopkg/cache.py", "demopkg/settings.py", "pyproject.toml"],
    "Добавлен файловый кэш get_cached() с настройкой CACHE_DIR; новая зависимость diskcache.",
    """--- a/demopkg/settings.py
+++ b/demopkg/settings.py
@@ -10,6 +10,7 @@ class Settings(BaseSettings):
     base_url: str = "https://api.example.com"
+    # читается из DEMOPKG_CACHE_DIR (см. docs/configuration.md)
+    cache_dir: str = ".demopkg-cache"
     page_size: int = 50
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -21,6 +21,7 @@ dependencies = [
     "pandas>=2.0",
+    "diskcache>=5.6",
 ]
--- a/demopkg/cache.py
+++ b/demopkg/cache.py
@@ new file mode 100644
+def get_cached(key, ttl_seconds=300):
+    \"\"\"Вернуть значение из файлового кэша по ключу key.\"\"\"
""",
    "add", ["docs/api-reference.md", "docs/configuration.md"], "Кэширование",
    """## Кэширование

Модуль `demopkg.cache` предоставляет файловый кэш на основе библиотеки `diskcache`.

#### get_cached(key, ttl_seconds=300)

Возвращает значение из кэша по строковому ключу `key`. Если записи нет или её
возраст превысил `ttl_seconds` (по умолчанию 300), возвращается `None`.

Настройки:

| Переменная окружения | По умолчанию | Описание |
|---|---|---|
| `DEMOPKG_CACHE_DIR` | `.demopkg-cache` | Каталог для файлов кэша. |

Новая обязательная зависимость: `diskcache>=5.6`.
""",
    ["get_cached", "ttl_seconds", "DEMOPKG_CACHE_DIR", "diskcache"],
    ["redis", "memcached", "cache_backend"],
    "ru", ["positive", "multi_file"], "medium",
    "Новый модуль + настройка + зависимость: документация в двух местах",
    None,
    "Проверяем связку config_or_env_changed и dependency_added: агент обязан задокументировать env-переменную и новую зависимость.",
    api_impact="added",
)

CONFIG_ENV_CHANGED_EN = case(
    "gold-006", "api_change", "write_docs", ["config_or_env_changed"],
    ["demopkg/settings.py"],
    "Переменная DEMOPKG_LOG_LEVEL теперь поддерживает значение JSON вместо текстового вывода логов.",
    """--- a/demopkg/settings.py
+++ b/demopkg/settings.py
@@ -22,7 +22,7 @@ class Settings(BaseSettings):
-    log_level: str = "INFO"   # DEBUG, INFO, WARNING; текстовый вывод
+    # DEMOPKG_LOG_LEVEL; JSON включает structured logging по одной записи на строку
+    log_level: str = "INFO"   # DEBUG, INFO, WARNING, JSON
     log_format: str = "text"
""",
    "update", ["docs/configuration.md"], "Environment variables / DEMOPKG_LOG_LEVEL",
    """#### `DEMOPKG_LOG_LEVEL`

Sets the verbosity of console logging. Accepted values: `DEBUG`, `INFO`,
`WARNING`, and the new `JSON` value which emits one structured JSON object
per line and disables the human-readable formatter.

```bash
export DEMOPKG_LOG_LEVEL=JSON   # machine-readable logs for log aggregators
```

When `JSON` is selected, `DEMOPKG_LOG_FORMAT` is ignored.
""",
    ["DEMOPKG_LOG_LEVEL", "JSON", "structured"],
    ["SYSLOG", "logrotate"],
    "en", ["positive"], "medium",
    "Изменение допустимых значений конфигурации",
    None,
    "Конфиг — часть публичного контракта. Тестируем, что агент не игнорирует правки комментариев/валидных значений.",
    api_impact="changed",
)

# ---------------------------------------------------------------- positive: feature_doc
FEATURE_TFLITE_EXAMPLES_RU = case(
    "gold-007", "feature_doc", "write_docs", ["new_user_feature"],
    ["demopkg/inference.py", "examples/image_classify.py"],
    "Добавлен класс ImageClassifier для локального инференса; приложен runnable-пример.",
    """--- a/demopkg/inference.py
+++ b/demopkg/inference.py
@@ new file mode 100644
+class ImageClassifier:
+    \"\"\"Локальная классификация изображений по модели ONNX.\"\"\"
+
+    def __init__(self, model_path, labels_path):
+        ...
+
+    def predict(self, image, top_k=3):
+        \"\"\"Вернуть top_k пар (метка, вероятность).\"\"\"
+        ...
--- a/examples/image_classify.py
+++ b/examples/image_classify.py
@@ new file mode 100644
+from demopkg.inference import ImageClassifier
+clf = ImageClassifier("model.onnx", "labels.txt")
+print(clf.predict("cat.jpg", top_k=2))
""",
    "add", ["docs/guide/inference.md"], "Классификация изображений",
    """## Классификация изображений

Модуль `demopkg.inference` позволяет запускать модель ONNX локально, без сервера.

```python
from demopkg.inference import ImageClassifier

clf = ImageClassifier("model.onnx", "labels.txt")
for label, score in clf.predict("cat.jpg", top_k=2):
    print(f"{label}: {score:.2f}")
```

Шаги подготовки:

1. Экспортируйте модель в формат ONNX и сохраните файл меток `labels.txt`
   (по одной метке на строку, порядок соответствует выходным индексам модели).
2. Создайте `ImageClassifier(model_path, labels_path)`.
3. Вызовите `predict(image, top_k=...)`, где `image` — путь к файлу или объект `PIL.Image.Image`.

`predict` возвращает список пар `(метка, вероятность)` длиной не более `top_k`,
отсортированный по убыванию вероятности.
""",
    ["ImageClassifier", "predict", "top_k", "model.onnx", "labels.txt"],
    ["tf.lite.Interpreter", "batch_predict", "GPU delegate"],
    "ru", ["positive", "needs_illustration", "hard_for_small_model"], "high",
    "Новая пользовательская возможность с runnable-примером",
    DONOR_TF + "/pull/128355 (добавление примеров TensorFlow Lite)",
    "Главный 'красивый' кейс для демо: эталон содержит код-пример и нумерованные шаги. "
    "Для 3b-модели сложный — ожидаем низкий LLM-judge балл и фиксируем это как риск.",
    api_impact="added",
)

FEATURE_BENCH_GUIDE_EN = case(
    "gold-008", "feature_doc", "write_docs", ["new_user_feature"],
    ["demopkg/bench.py"],
    "Добавлен CLI `python -m demopkg.bench` для измерения пропускной способности экспорта.",
    """--- a/demopkg/bench.py
+++ b/demopkg/bench.py
@@ new file mode 100644
+def main(argv=None):
+    \"\"\"Run an export benchmark and print rows/sec.\"\"\"
+    parser = argparse.ArgumentParser(prog="python -m demopkg.bench")
+    parser.add_argument("--rows", type=int, default=100_000)
+    parser.add_argument("--format", choices=["csv", "parquet"], default="parquet")
+    parser.add_argument("--output", default="/tmp/bench")
""",
    "add", ["docs/guide/benchmarking.md"], "Benchmarking CLI",
    """## Benchmarking exports

The package ships a small CLI that measures export throughput so you can
compare formats on your own hardware before committing to one.

```bash
python -m demopkg.bench --rows 100000 --format parquet --output /tmp/bench
```

| Flag | Default | Description |
|---|---|---|
| `--rows` | `100000` | Number of rows to generate for the benchmark. |
| `--format` | `parquet` | Export format under test: `csv` or `parquet`. |
| `--output` | `/tmp/bench` | Directory where intermediate files are written. |

The command prints elapsed time and rows/sec. Results depend heavily on disk
speed, so run the benchmark at least three times and keep the median.
""",
    ["python -m demopkg.bench", "--rows", "--format", "rows/sec"],
    ["--threads", "pytest-benchmark"],
    "en", ["positive", "needs_illustration"], "medium",
    "Новый CLI-инструмент: нужен раздел руководства с флагами",
    None,
    "CLI-документация требует точного перечисления флагов и значений по умолчанию — хорошая проверка faithfulness.",
)

FEATURE_USER_WORKFLOW_RU = case(
    "gold-009", "feature_doc", "write_docs", ["new_user_feature", "behavior_changed"],
    ["demopkg/session.py"],
    "Появился контекстный менеджер Session: автозакрытие соединения и повторное использование токена.",
    """--- a/demopkg/session.py
+++ b/demopkg/session.py
@@ -18,6 +18,14 @@ class Session:
     def __init__(self, token):
         self.token = token
+
+    def __enter__(self):
+        self.connect()
+        return self
+
+    def __exit__(self, exc_type, exc, tb):
+        self.close()
+        return False
""",
    "update", ["docs/guide/getting-started.md"], "Работа с сессией",
    """### Работа с сессией

Соединение лучше открывать через контекстный менеджер — тогда оно гарантированно
закрывается даже при исключении:

```python
from demopkg.session import Session

with Session(token="...") as session:
    rows = session.client.fetch_records("users", timeout=5.0)
```

Ручное управление тоже доступно: вызовите `session.connect()` и затем
`session.close()`. Объект `Session` повторно использует токен для всех запросов
внутри блока, поэтому повторная авторизация на каждый вызов не выполняется.
""",
    ["with Session", "connect()", "close()", "токен"],
    ["session.restart()", "thread-safe"],
    "ru", ["positive"], "medium",
    "Новый способ использования существующего класса",
    DONOR_QGIS + "/pull/7887 (обновление раздела руководства)",
    "Проверяем обновление существующего раздела, а не создание нового — важность пересечения changed-files x docs-references.",
)

FEATURE_EXPORT_STREAMING_EN = case(
    "gold-010", "feature_doc", "write_docs", ["new_user_feature", "api_new"],
    ["demopkg/exporters.py"],
    "Добавлен генератор iter_export_chunks() для выгрузки больших датасетов частями.",
    """--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -30,6 +30,14 @@ def export_to_parquet(df, path, compression="snappy"):
     df.to_parquet(path, compression=compression)
 
 
+def iter_export_chunks(df, chunksize=10_000):
+    \"\"\"Streaming generator: yields DataFrames of at most `chunksize` rows.
+
+    The last chunk may be shorter than requested.
+    \"\"\"
+    for start in range(0, len(df), chunksize):
+        yield df.iloc[start : start + chunksize]
""",
    "add", ["docs/guide/large-datasets.md"], "Streaming export",
    """## Streaming export of large datasets

Loading a whole dataset into memory before exporting it is not always
possible. `iter_export_chunks` yields slices of the source DataFrame so each
chunk can be written and released independently:

```python
from demopkg.exporters import iter_export_chunks, export_to_parquet

for i, chunk in enumerate(iter_export_chunks(df, chunksize=10_000)):
    export_to_parquet(chunk, f"out/part-{i}.parquet")
```

`chunksize` defaults to `10_000` rows. The function is a generator, so no
memory is allocated until the first chunk is requested, and the final chunk may
contain fewer rows than requested.
""",
    ["iter_export_chunks", "chunksize", "generator", "10_000"],
    ["multiprocessing", "async for"],
    "en", ["positive"], "medium",
    "Новый потоковый API для больших данных",
    None,
    "Генераторная семантика (ленивость, последний чанк короче) — проверяем точность формулировок, а не только список параметров.",
    api_impact="added",
)

# ---------------------------------------------------------------- positive: adr_impact
ADR_NEW_BACKEND_RU = case(
    "gold-011", "adr_impact", "write_docs", ["adr_new", "dependency_added"],
    ["demopkg/storage/base.py", "demopkg/storage/local.py", "pyproject.toml"],
    "Введён слой хранилища с интерфейсом StorageBackend; выбор бэкенда через фабрику.",
    """--- a/demopkg/storage/base.py
+++ b/demopkg/storage/base.py
@@ new file mode 100644
+class StorageBackend(abc.ABC):
+    @abc.abstractmethod
+    def put(self, key, payload): ...
+    @abc.abstractmethod
+    def get(self, key): ...
--- a/demopkg/storage/__init__.py
+++ b/demopkg/storage/__init__.py
+def create_backend(name="local"):
+    \"\"\"Return a StorageBackend implementation by name.\"\"\"
+    return LOCAL_BACKENDS[name]()
""",
    "add", ["docs/adr/0004-storage-backend-abstraction.md"], "ADR-0004",
    """# ADR 0004. Абстракция слоя хранения

Дата: 2026-10-05  
Статус: принят

## Контекст

Пакет писался с предположением, что данные всегда лежат на локальном диске.
Появились требования хранить артефакты в объектном хранилище, и прямые вызовы
`open()` начали дублироваться в модуле экспорта и в кэше.

## Решение

Вводится интерфейс `StorageBackend` (`put`, `get`) и фабрика
`create_backend(name="local")`. Вся работа с данными идёт только через
абстракцию; конкретные реализации регистрируются в `LOCAL_BACKENDS`.

## Последствия

* (+) Локальный диск остаётся реализацией по умолчанию — обратная совместимость сохранена.
* (+) Новый бэкенд добавляется без правок потребительского кода.
* (-) Две дополнительные точки отказа: фабрика и реестр реализаций.
* (-) Отладка усложняется: стек вызовов стал длиннее, чем при прямом `open()`.
""",
    ["StorageBackend", "create_backend", "ADR", "Контекст", "Решение", "Последствия"],
    ["S3", "grpc", "микроcервис"],
    "ru", ["positive", "hard_for_small_model"], "high",
    "Новое архитектурное решение: требуется запись ADR",
    None,
    "ADR — самый сложный жанр для 3b: нужен баланс контекста и последствий. Ключевой кейс для LLM-judge rubric.",
    api_impact="added",
)

ADR_MODEL_SWITCH_EN = case(
    "gold-012", "adr_impact", "write_docs", ["adr_changed", "dependency_added"],
    ["demopkg/llm/backend.py", "pyproject.toml"],
    "LLM-бэкенд переведён с прямого вызова Ollama на OpenAI-совместимый клиент: один код для Ollama и vLLM.",
    """--- a/demopkg/llm/backend.py
+++ b/demopkg/llm/backend.py
@@ -5,10 +5,10 @@
-import ollama
+from openai import OpenAI
 
-def generate(prompt, model="qwen2.5-coder:3b"):
-    return ollama.generate(model=model, prompt=prompt)["response"]
+# единый клиент для Ollama и vLLM (OpenAI-совместимый endpoint)
+def generate(prompt, model="qwen2.5-coder:3b", base_url=None):
+    client = OpenAI(base_url=base_url or os.environ["OPENAI_BASE_URL"], api_key="unused")
+    return client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}]).choices[0].message.content
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -22,7 +22,7 @@ dependencies = [
-    "ollama>=0.3",
+    "openai>=1.40",
 ]
""",
    "update", ["docs/adr/0002-llm-backend.md"], "ADR-0002 / Consequences",
    """## Update (2026-10-05): OpenAI-compatible client instead of native bindings

The original decision used the `ollama` Python package directly. That made the
vLLM deployment path a second implementation with its own bugs.

We now talk to every backend through the OpenAI-compatible HTTP API and select
the endpoint with the `OPENAI_BASE_URL` environment variable:

| Backend | `OPENAI_BASE_URL` |
|---|---|
| Ollama (local dev) | `http://localhost:11434/v1` |
| vLLM (docker) | `http://vllm:8000/v1` |

Consequences: one code path for both backends; the `ollama` dependency is
dropped; `api_key` is still required by the client but is never validated
locally, so a placeholder value is acceptable.
""",
    ["OPENAI_BASE_URL", "OpenAI-compatible", "vLLM", "ollama"],
    ["LangChain", "litellm"],
    "en", ["positive", "edge"], "high",
    "Изменение ранее принятого архитектурного решения",
    None,
    "ADR amend, а не новый документ: агент должен дополнить существующий файл, а не создать ADR-000N. Проверяем target_section.",
    api_impact="changed",
)

ADR_DB_CHOICE_RU = case(
    "gold-013", "adr_impact", "write_docs", ["adr_new", "breaking_change"],
    ["demopkg/state/store.py", "pyproject.toml"],
    "Хранилище состояния графа: SQLite вместо JSON-файла; для production заложен интерфейс под Postgres.",
    """--- a/demopkg/state/store.py
+++ b/demopkg/state/store.py
@@ -1,9 +1,12 @@
-import json
+import sqlite3
+
+# MVP: SQLite. Интерфейс рассчитан на реализацию поверх PostgreSQL в production.
+SCHEMA = "CREATE TABLE IF NOT EXISTS state (run_id TEXT PRIMARY KEY, payload BLOB)"
 
-def load_state(path):
-    with open(path) as fh:
-        return json.load(fh)
+def load_state(db_path, run_id):
+    con = sqlite3.connect(db_path)
+    row = con.execute("SELECT payload FROM state WHERE run_id = ?", (run_id,)).fetchone()
+    return pickle.loads(row[0]) if row else None
""",
    "add", ["docs/adr/0005-checkpoint-storage.md"], "ADR-0005",
    """# ADR 0005. Хранилище чекпоинтов состояния

Дата: 2026-10-05  
Статус: принят

## Контекст

Состояние прерванного выполнения сохранялось в JSON-файл. При параллельных
запусках записи терялись, а повреждённый файл блокировал весь пайплайн.

## Решение

Чекпоинты хранятся в SQLite: таблица `state(run_id, payload)`, сериализация —
pickle. Интерфейс `load_state(db_path, run_id)` описан так, чтобы его можно было
реализовать поверх PostgreSQL без изменения вызывающего кода.

## Последствия

* (+) Атомарные записи, отсутствие гонок при конкурентных запусках.
* (+) Миграция на Postgres для production сводится к новой реализации интерфейса.
* (-) Формат JSON больше не читается человеком; отладка требует `sqlite3`.
* (-) Breaking change: подпись `load_state()` изменена, вызовы `load_state(path)` невалидны.
""",
    ["SQLite", "run_id", "PostgreSQL", "Breaking change"],
    ["Redis", "MongoDB", "WAL"],
    "ru", ["positive", "breaking"], "medium",
    "Смена подсистемы хранения состояния",
    None,
    "Комбинированный кейс: ADR + предупреждение о ломающем изменении сигнатуры в том же PR.",
    api_impact="changed",
)

ADR_DEPS_AUDIT_EN = case(
    "gold-014", "adr_impact", "write_docs", ["adr_changed", "dependency_added"],
    ["requirements-dev.txt", "Makefile"],
    "В CI добавлен pip-audit; политика безопасности требует проверки зависимостей перед релизом.",
    """--- a/Makefile
+++ b/Makefile
@@ -14,6 +14,9 @@ lint:
 	ruff check src tests
+
+audit:
+	pip-audit -r requirements.txt --strict
--- a/requirements-dev.txt
+++ b/requirements-dev.txt
@@ -3,3 +3,4 @@ pytest>=8.0
 ruff>=0.5
+pip-audit>=2.7
""",
    "update", ["docs/adr/0003-dependency-policy.md"], "Dependency policy / auditing",
    """## Auditing (added 2026-10-05)

The policy previously covered only version pinning. It now also requires a
vulnerability audit of the runtime dependency set before every release.

```make
make audit   # runs: pip-audit -r requirements.txt --strict
```

Rules:

* `pip-audit` must pass with `--strict`; a failed audit blocks the release tag.
* Dev-only tools live in `requirements-dev.txt` and are not part of the audited runtime set.
* Exceptions require a comment in `docs/adr/0003-dependency-policy.md` naming the advisory ID.
""",
    ["pip-audit", "--strict", "requirements-dev.txt"],
    ["safety", "dependabot auto-merge"],
    "en", ["positive"], "low",
    "Изменение политики зависимостей/безопасности",
    None,
    "Не все изменения CI — noise: правка, влияющая на политику, должна попадать в ADR. Пограничный кейс против правила 'ci_test_only => молчать'.",
)

# ---------------------------------------------------------------- positive: readme_update
README_INSTALL_RU = case(
    "gold-015", "readme_update", "write_docs", ["config_or_env_changed", "dependency_added"],
    ["pyproject.toml", "demopkg/settings.py"],
    "Требуемая версия Python поднята до 3.11, добавлена обязательная переменная DEMOPKG_API_TOKEN.",
    """--- a/pyproject.toml
+++ b/pyproject.toml
@@ -8,7 +8,7 @@
 [project]
 name = "demopkg"
-requires-python = ">=3.9"
+requires-python = ">=3.11"
 dependencies = ["pandas>=2.0", "openai>=1.40"]
--- a/demopkg/settings.py
+++ b/demopkg/settings.py
@@ -12,6 +12,7 @@ class Settings(BaseSettings):
+    api_token: str  # обязательное поле, читается из DEMOPKG_API_TOKEN
     page_size: int = 50
""",
    "update", ["README.md"], "Установка",
    """## Установка

Требуется Python 3.11 или новее.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install demopkg
```

Перед первым запуском обязательно задайте токен доступа — без него приложение
не стартует:

```bash
export DEMOPKG_API_TOKEN="<ваш токен>"
```
""",
    ["Python 3.11", "DEMOPKG_API_TOKEN", "pip install demopkg"],
    ["conda", "poetry install"],
    "ru", ["positive"], "high",
    "Изменение системных требований: правка README",
    DONOR_TF + "/pull/128320 (правки в справочном разделе)",
    "README — самая заметная часть документации. Пропуск такого кейса особенно дорог: проверяем recall.",
    api_impact="changed",
)

README_QUICKSTART_EN = case(
    "gold-016", "readme_update", "write_docs", ["new_user_feature"],
    ["demopkg/__init__.py"],
    "Публичный вход в пакет изменён: рекомендуемый способ — from demopkg import Client.",
    """--- a/demopkg/__init__.py
+++ b/demopkg/__init__.py
@@ -1,6 +1,8 @@
-from .client import APIClient
+from .client import APIClient as Client
+from .exporters import export_to_csv, export_to_parquet
 
-__all__ = ["APIClient"]
+__version__ = "0.4.0"
+__all__ = ["Client", "export_to_csv", "export_to_parquet", "__version__"]
""",
    "update", ["README.md"], "Quickstart",
    """## Quickstart

```python
from demopkg import Client, export_to_parquet

with Client(...) as app:          # alias for demopkg.client.APIClient
    rows = app.fetch_records("users", timeout=5.0)
    export_to_parquet(rows, "users.parquet")
```

`APIClient` is still importable from `demopkg.client`, but `demopkg.Client` is
the documented entry point as of version 0.4.0.
""",
    ["from demopkg import Client", "0.4.0", "export_to_parquet"],
    ["import demopkg as dp", "demopkg.connect()"],
    "en", ["positive", "edge"], "medium",
    "Смена рекомендуемой точки входа пакета",
    None,
    "Alias без удаления старого имени: агент должен показать и новый канон, и обратную совместимость.",
    api_impact="changed",
)

README_BADGES_PY311_EN = case(
    "gold-017", "readme_update", "write_docs", ["config_or_env_changed"],
    ["README.md", ".github/workflows/ci.yml", "pyproject.toml"],
    "Бейджи и матрица CI версий рассогласованы с requires-python=3.11 — README врёт о поддержке 3.8.",
    """--- a/README.md
+++ b/README.md
@@ -3,7 +3,7 @@
-![python](https://img.shields.io/badge/python-3.8%2B-blue)
+![python](https://img.shields.io/badge/python-3.11%2B-blue)
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -10,7 +10,7 @@ jobs:
     strategy:
       matrix:
-        python-version: ["3.8", "3.9", "3.10"]
+        python-version: ["3.11", "3.12"]
""",
    "update", ["README.md"], "Badges / Supported versions",
    """### Supported versions

The project is tested on Python 3.11 and 3.12 only. The badge and the CI
matrix were both out of date: `pyproject.toml` declares `requires-python =
">=3.11"`, so installations on 3.8–3.10 fail at import time even though the old
badge advertised them.
""",
    ["3.11", "3.12", "requires-python"],
    ["3.8 supported", "3.9"],
    "en", ["positive", "multi_file", "edge"], "medium",
    "Устаревшая информация о поддерживаемых версиях",
    None,
    "Кейс 'код меняется, а README отстаёт' — главный сценарий ценности системы. Требует cross-file проверки (CI x pyproject x README).",
)

README_CONTRIB_SETUP_RU = case(
    "gold-018", "readme_update", "write_docs", ["dependency_added", "config_or_env_changed"],
    ["Makefile", "CONTRIBUTING.md"],
    "Для разработки нужен Docker (compose для Ollama) и команда make dev-setup.",
    """--- a/Makefile
+++ b/Makefile
@@ -6,6 +6,9 @@ help:
+# backend модели выбирается через DOCS_BACKEND=ollama|vllm (по умолчанию ollama)
+dev-setup:
+	docker compose up -d ollama
+	pip install -e ".[dev]"
+
 test:
 	pytest
""",
    "update", ["docs/contributing.md"], "Локальное окружение разработки",
    """## Локальное окружение разработки

Для запуска тестов и агента локально нужен Docker — в нём поднимаются контейнеры
Ollama с моделью `qwen2.5-coder:3b`.

```bash
make dev-setup      # docker compose up -d ollama + pip install -e ".[dev]"
make test
```

После установки проверьте доступность модели: `curl http://localhost:11434/api/tags`.
Переключение на vLLM выполняется переменными окружения без изменения кода
(`DOCS_BACKEND=vllm`, `OPENAI_BASE_URL=http://localhost:8000/v1`).
""",
    ["make dev-setup", "docker compose", "qwen2.5-coder:3b", "DOCS_BACKEND"],
    ["npm install", "virtualenvwrapper"],
    "ru", ["positive"], "low",
    "Изменение процесса настройки окружения разработчика",
    None,
    "Инфраструктурные изменения тоже часть контракта для контрибьюторов — но приоритет low, агент может предложить короткую правку.",
)

README_CHANGELOG_SECTION_EN = case(
    "gold-019", "readme_update", "write_docs", ["new_user_feature", "api_new"],
    ["CHANGELOG.md", "demopkg/exporters.py"],
    "Релиз 0.4.0: добавлены Parquet-экспорт и бенчмарк CLI — нужен раздел Release notes в README.",
    """--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -1,5 +1,11 @@
 # Changelog
+## 0.4.0 - 2026-10-05
+### Added
+- `export_to_parquet(df, path, compression="snappy")`
+- `python -m demopkg.bench` CLI
+### Changed
+- **Breaking**: `APIClient.fetch_records` now requires `timeout`
""",
    "add", ["README.md"], "Release notes",
    """## Release notes — 0.4.0

Highlights:

* New Parquet exporter: `export_to_parquet(df, path, compression="snappy")` —
  smaller files than CSV and lossless column types.
* New benchmark CLI: `python -m demopkg.bench --rows 100000 --format parquet`.
* Breaking: `APIClient.fetch_records` now requires the `timeout` argument.

The full list of changes lives in `CHANGELOG.md`.
""",
    ["0.4.0", "export_to_parquet", "demopkg.bench", "Breaking"],
    ["0.5.0", "yanked"],
    "en", ["positive", "multi_file"], "medium",
    "Сводка релиза в README по данным CHANGELOG",
    None,
    "Агент обязан синхронизировать формулировки между CHANGELOG и README и не выдумывать следующий релиз.",
)

README_VERSION_PIN_EN = case(
    "gold-020", "readme_update", "write_docs", ["behavior_changed", "breaking_change"],
    ["docs/guide/install.md"],
    "В руководстве рекомендуется ставить пакет без фиксации версии, хотя API ломается в минорных релизах.",
    """--- a/docs/guide/install.md
+++ b/docs/guide/install.md
@@ -7,7 +7,7 @@
-pip install demopkg
+pip install "demopkg==0.4.0"
 """,
    "update", ["README.md"], "Installation",
    """## Installation

Pin the version explicitly. The public API receives breaking changes in minor
releases (see `docs/adr/0006-api-stability.md`), so an unpinned `pip install
demopkg` can break your code on the next upgrade:

```bash
pip install "demopkg==0.4.0"
```

To upgrade deliberately, change the pin in `requirements.txt` and re-run the
test suite.
""",
    ["Pin the version", "demopkg==0.4.0", "breaking changes"],
    ["pip install -U demopkg", "latest"],
    "en", ["positive", "edge"], "low",
    "Правка рекомендаций по установке в существующем руководстве",
    None,
    "Docs-only правка в одном файле должна быть подхвачена и в README — проверка правила 'docs-references пересечение'.",
)

# ---------------------------------------------------------------- negative: typo_fix
TYPO_README_RU = case(
    "gold-021", "typo_fix", "stay_silent", ["none"],
    ["README.md"],
    "Исправлены две опечатки в тексте README, смысл и API не изменились.",
    """--- a/README.md
+++ b/README.md
@@ -12,7 +12,7 @@
-Демонстрационный паке для работы с данными.
+Демонстрационный пакет для работы с данными.
@@ -30,7 +30,7 @@
-Установка выполяется через pip.
+Установка выполняется через pip.
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "noise"], "high",
    "Орфография в README",
    DONOR_TF + "/pull/128320 (исправление опечаток в API reference)",
    "Anti-spam кейс высшего приоритета: система не должна плодить PR ради опечаток. Порог: diff < 5 строк и только текст.",
)

TYPO_DOCSTRING_EN = case(
    "gold-022", "typo_fix", "stay_silent", ["none"],
    ["demopkg/exporters.py"],
    "В docstring исправлено 'recieve' -> 'receive'; поведение функции не тронуто.",
    """--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -8,7 +8,7 @@ def export_to_csv(df, path, sep=","):
-    \"\"\"Write a DataFrame and recieve the target path back.\"\"\"
+    \"\"\"Write a DataFrame and receive the target path back.\"\"\"
""",
    "none", [], None, None, [], [],
    "en", ["negative", "noise"], "high",
    "Опечатка в docstring",
    None,
    "Даже если мы следим за docstring как источником для API-ref, одна орфографическая правка не повод создавать draft PR.",
)

TYPO_DOCS_PROSE_RU = case(
    "gold-023", "typo_fix", "stay_silent", ["none"],
    ["docs/guide/getting-started.md"],
    "Расстановка запятых и исправление 'так-же' -> 'так же' в руководстве.",
    """--- a/docs/guide/getting-started.md
+++ b/docs/guide/getting-started.md
@@ -20,7 +20,7 @@
-После установки так-же нужно проверить, что токен задан
+После установки, так же нужно проверить, что токен задан
@@ -44,7 +44,7 @@
-Запрос отправляеться автоматически
+Запрос отправляется автоматически
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "noise"], "medium",
    "Пунктуация и орфография в prose-документации",
    DONOR_QGIS + "/pull/7934 (исправление опечаток в руководстве пользователя)",
    "Пограничный: правки в самой docs/. Правило — не комментировать чисто орфографические изменения независимо от того, где они сделаны.",
)

TYPO_VARIABLE_NAMING_EN = case(
    "gold-024", "typo_fix", "stay_silent", ["none"],
    ["demopkg/session.py"],
    "Исправлена опечатка в имени приватного атрибута _toke -> _token (внутреннее поле, не API).",
    """--- a/demopkg/session.py
+++ b/demopkg/session.py
@@ -21,8 +21,8 @@ class Session:
     def connect(self):
-        self._toke = self._read_toke()
-        return self._auth(self._toke)
+        self._token = self._read_token()
+        return self._auth(self._token)
""",
    "none", [], None, None, [], [],
    "en", ["negative", "edge"], "high",
    "Переименование приватного атрибута с опечаткой",
    None,
    "Ловушка: имя символа изменилось, но символ приватный (_prefix) и не входит в __all__ — публичный контракт не затронут.",
)

# ---------------------------------------------------------------- negative: internal_refactor
REFACTOR_PRIVATE_EN = case(
    "gold-025", "internal_refactor", "stay_silent", ["none"],
    ["demopkg/_internal/parser.py"],
    "Внутренний парсер разбит на приватные хелперы; публичные сигнатуры не менялись.",
    """--- a/demopkg/_internal/parser.py
+++ b/demopkg/_internal/parser.py
@@ -14,12 +14,16 @@ def parse_response(raw):
-    items = []
-    for row in raw.splitlines():
-        if not row.strip():
-            continue
-        items.append(row.split("\\t"))
-    return items
+    return [cells for cells in (_split(row) for row in _nonempty_lines(raw))]
+
+
+def _nonempty_lines(raw):
+    return (line for line in raw.splitlines() if line.strip())
+
+
+def _split(line):
+    return line.split("\\t")
""",
    "none", [], None, None, [], [],
    "ru", ["negative"], "high",
    "Рефакторинг внутреннего модуля _internal",
    None,
    "Папка _internal + все новые имена с подчеркиванием — эталонный negative для эвристики публичного API.",
    api_impact="none",
)

REFACTIR_TYPE_HINTS_RU = case(
    "gold-026", "internal_refactor", "stay_silent", ["none"],
    ["demopkg/client.py", "demopkg/exporters.py"],
    "Добавлены аннотации типов и from __future__ import annotations; рантайм-поведение идентично.",
    """--- a/demopkg/client.py
+++ b/demopkg/client.py
@@ -1,4 +1,5 @@
+from __future__ import annotations
 import requests
@@ -30,7 +31,7 @@ class APIClient:
-    def fetch_records(self, endpoint, timeout, limit=100):
+    def fetch_records(self, endpoint: str, timeout: float, limit: int = 100) -> list[dict]:
--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -10,7 +11,7 @@
-def export_to_csv(df, path, sep=","):
+def export_to_csv(df: pd.DataFrame, path: str, sep: str = ",") -> None:
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "multi_file"], "medium",
    "Type hints без изменения контракта",
    None,
    "Сигнатуры 'изменились' текстово, но не семантически. Тестируем способность отличить типизацию от смены API — один из самых частых источников ложных срабатываний.",
    api_impact="none",
)

REFACTOR_MOVE_MODULE_EN = case(
    "gold-027", "internal_refactor", "stay_silent", ["none"],
    ["demopkg/utils/text.py", "demopkg/text.py"],
    "Приватный модуль utils.text перемещён в text; публичные реэкспорты не менялись.",
    """--- a/demopkg/utils/text.py
+++ /dev/null
@@ -1,6 +0,0 @@
-def _strip_accents(value):
-    ...
--- /dev/null
+++ b/demopkg/text.py
@@ new file mode 100644
+def _strip_accents(value):
+    ...
""",
    "none", [], None, None, [], [],
    "en", ["negative", "edge"], "medium",
    "Перемещение приватного модуля",
    None,
    "Rename/delete файлов выглядит драматично для diff-статистики. Проверяем, что префильтр смотрит на содержимое, а не на объём изменений.",
    api_impact="none",
)

REFACTOR_TEST_ONLY_RU = case(
    "gold-028", "internal_refactor", "stay_silent", ["none"],
    ["tests/test_exporters.py"],
    "Тесты переписаны с unittest-подобных assert на pytest.raises; код пакета не менялся.",
    """--- a/tests/test_exporters.py
+++ b/tests/test_exporters.py
@@ -5,8 +5,8 @@ def test_export_to_csv(tmp_path):
-    try:
-        export_to_csv(df, tmp_path / "out.csv")
-    except Exception as exc:
-        assert False, exc
+    export_to_csv(df, tmp_path / "out.csv")
+    assert (tmp_path / "out.csv").exists()
""",
    "none", [], None, None, [], [],
    "ru", ["negative"], "medium",
    "Изменения только в тестах",
    None,
    "Правило 'tests => молчим'. Важный negative: большой diff при нулевом влиянии на пользователя.",
)

REFACTOR_PERF_INTERNAL_EN = case(
    "gold-029", "internal_refactor", "stay_silent", ["none"],
    ["demopkg/cache.py"],
    "Внутренняя оптимизация: словарь вместо списка при поиске ключа; публичный get_cached() не изменился.",
    """--- a/demopkg/cache.py
+++ b/demopkg/cache.py
@@ -18,9 +18,9 @@ _INDEX = []
-def _find(key):
-    for entry in _INDEX:
-        if entry[0] == key:
-            return entry[1]
+def _find(key):
+    for entry in _INDEX.values():
+        if entry["key"] == key:
+            return entry["value"]
     return None
""",
    "none", [], None, None, [], [],
    "en", ["negative", "edge"], "medium",
    "Внутренняя оптимизация производительности",
    None,
    "Спорный кейс: пользователь может заметить ускорение. Эталон — молчать, потому что контракт и API-ref не меняются. Помечаем как edge для последующего пересмотра порога.",
    api_impact="none",
)

# ---------------------------------------------------------------- negative: ci_test_only
CI_WORKFLOW_MATRIX_EN = case(
    "gold-030", "ci_test_only", "stay_silent", ["none"],
    [".github/workflows/ci.yml"],
    "В CI добавлен job с кэшем pip и поднят timeout сборки.",
    """--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -12,6 +12,10 @@ jobs:
     steps:
       - uses: actions/checkout@v4
+      - uses: actions/cache@v4
+        with:
+          path: ~/.cache/pip
+          key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements.txt') }}
       - run: pip install -e ".[dev]"
""",
    "none", [], None, None, [], [],
    "en", ["negative", "noise"], "medium",
    "Изменения только в конфигурации CI",
    None,
    "Явное правило IGNORE из §6 плана. Частый источник спама в реальных репозиториях.",
)

CI_ADD_COVERAGE_RU = case(
    "gold-031", "ci_test_only", "stay_silent", ["none"],
    ["pyproject.toml", ".github/workflows/ci.yml"],
    "Включён coverage с порогом 80%, отчёт публикуется в CI.",
    """--- a/pyproject.toml
+++ b/pyproject.toml
@@ -40,3 +40,7 @@
 [tool.coverage.run]
+source = ["demopkg"]
+fail_under = 80
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -20,3 +20,5 @@
+      - run: pytest --cov --cov-report=xml
+      - uses: codecov/codecov-action@v4
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "multi_file"], "low",
    "Инструментарий покрытия кода",
    None,
    "Изменение в pyproject.toml обычно триггерит внимание — здесь это tool-секция, не [project]. Проверяем аккуратность парсинга pyproject.",
)

CI_FIX_FLAKY_TEST_EN = case(
    "gold-032", "ci_test_only", "stay_silent", ["none"],
    ["tests/test_session.py"],
    "Стабилизация flaky-теста: увеличен таймаут ожидания и добавлен seed.",
    """--- a/tests/test_session.py
+++ b/tests/test_session.py
@@ -22,7 +22,8 @@ def test_session_reconnect():
-    time.sleep(0.1)
+    random.seed(42)
+    time.sleep(1.0)
     assert session.is_connected
""",
    "none", [], None, None, [], [],
    "en", ["negative", "noise"], "low",
    "Исправление нестабильного теста",
    None,
    "Ноль влияния на пользователя, несмотря на слово 'fix' в названии. Тестируем устойчивость к ключевым словам коммита.",
)

CI_PRECOMMIT_CONFIG_RU = case(
    "gold-033", "ci_test_only", "stay_silent", ["none"],
    [".pre-commit-config.yaml"],
    "Добавлен хук mypy в pre-commit.",
    """--- a/.pre-commit-config.yaml
+++ b/.pre-commit-config.yaml
@@ -8,3 +8,7 @@ repos:
       - id: ruff
+  - repo: https://github.com/pre-commit/mirrors-mypy
+    rev: v1.11.0
+    hooks:
+      - id: mypy
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "noise"], "low",
    "Конфигурация pre-commit",
    None,
    "Отсутствие изменений в коде пакета вообще — простейший negative, но важен для статистики precision на шумовых PR.",
)

# ---------------------------------------------------------------- negative: style_format_only
STYLE_BLACK_DIFF_EN = case(
    "gold-034", "style_format_only", "stay_silent", ["none"],
    ["demopkg/client.py", "demopkg/exporters.py", "demopkg/pipeline.py"],
    "Прогнан ruff format: переносы строк и кавычки; логика не менялась.",
    """--- a/demopkg/client.py
+++ b/demopkg/client.py
@@ -44,9 +44,11 @@ class APIClient:
-        url = f"{self.base_url}/{endpoint}"
-        resp = self.session.get(url, params={"limit": limit}, timeout=timeout)
+        url = (
+            f"{self.base_url}/{endpoint}"
+        )
+        resp = self.session.get(
+            url, params={"limit": limit}, timeout=timeout
+        )
""",
    "none", [], None, None, [], [],
    "en", ["negative", "multi_file", "noise"], "high",
    "Массовое форматирование кода",
    None,
    "Опасный кейс: огромный diff при нулевом смысловом изменении. Проверка того, что префильтр сравнивает AST/токены, а не строки.",
    api_impact="none",
)

STYLE_LINT_FIX_RU = case(
    "gold-035", "style_format_only", "stay_silent", ["none"],
    ["demopkg/inference.py"],
    "Исправлены lint-замечания: неиспользуемый импорт удалён, f-string без плейсхолдеров упрощён.",
    """--- a/demopkg/inference.py
+++ b/demopkg/inference.py
@@ -1,7 +1,6 @@
 import numpy as np
-import os
@@ -33,7 +32,7 @@ class ImageClassifier:
-        msg = f"no labels"
+        msg = "no labels"
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "noise"], "medium",
    "Lint-only правки",
    None,
    "Изменение строк есть, контракта нет. Классический шум.",
    api_impact="none",
)

STYLE_DOCSTRING_REFORMAT_EN = case(
    "gold-036", "style_format_only", "stay_silent", ["none"],
    ["demopkg/exporters.py"],
    "Docstring переформатирован под Google-стиль: те же слова, другие отступы и заголовки секций.",
    """--- a/demopkg/exporters.py
+++ b/demopkg/exporters.py
@@ -30,10 +30,12 @@ def export_to_parquet(df, path, compression="snappy"):
-    \"\"\"Сохранить DataFrame в формате Parquet.
-    df: данные.
-    path: куда писать.
-    compression: algo, snappy по умолчанию.
-    \"\"\"
+    \"\"\"Сохранить DataFrame в формате Parquet.
+
+    Args:
+        df: данные.
+        path: куда писать.
+        compression: algo, snappy по умолчанию.
+    \"\"\"
""",
    "none", [], None, None, [], [],
    "en", ["negative", "edge"], "high",
    "Реформатирование docstring без смены смысла",
    None,
    "Самый тонкий negative: трогает документацию, но не содержание. Агент не должен открывать PR 'потому что docstring изменился'.",
    api_impact="none",
)

STYLE_RENAME_LOCAL_VAR_EN = case(
    "gold-037", "style_format_only", "stay_silent", ["none"],
    ["demopkg/pipeline.py"],
    "Локальные переменные переименованы для читаемости внутри resize_batch; публичный API и поведение не менялись.",
    """--- a/demopkg/pipeline.py
+++ b/demopkg/pipeline.py
@@ -52,9 +52,9 @@ def resize_batch(images, size, mode="bilinear"):
-        for i in range(size):
-            tmp = _pad(images[i])
-            out.append(_resize(tmp, size, mode))
+        for idx in range(size):
+            padded = _pad(images[idx])
+            out.append(_resize(padded, size, mode))
""",
    "none", [], None, None, [], [],
    "en", ["negative", "edge", "noise"], "medium",
    "Переименование локальных переменных",
    None,
    "Ловушка на поверхностный матчинг имён: в diff появляются новые идентификаторы (idx, padded), которых нет ни в коде вокруг, ни в документации. Детектор обязан различать scope: имя локальной переменной не является частью контракта и не требует правки docs.",
    api_impact="none",
)

# ---------------------------------------------------------------- special: docs_only
DOCS_ONLY_NEW_SECTION_RU = case(
    "gold-038", "docs_only", "stay_silent", ["none"],
    ["docs/guide/inference.md"],
    "Автор сам добавил раздел документации без изменений в коде.",
    """--- a/docs/guide/inference.md
+++ b/docs/guide/inference.md
@@ -60,6 +60,14 @@
+### Ограничения модели
+
+* Поддерживаются только модели с одним входом.
+* Размер входного изображения фиксирован — 224x224.
""",
    "none", [], None, None, [], [],
    "ru", ["negative", "edge"], "high",
    "PR только с правками документации",
    DONOR_QGIS + "/pull/7887 (обновление раздела пользователем)",
    "Важное уточнение semantics: stay_silent здесь означает 'не создавать свой draft PR', а не 'проигнорировать PR'. Корректное поведение — один короткий служебный комментарий 'код не затронут, документация обновлена автором' и никаких правок. Разрешено отдельным конфигом docs_only_comment=true.",
)

DOCS_ONLY_LINK_FIX_EN = case(
    "gold-039", "docs_only", "stay_silent", ["none"],
    ["docs/api-reference.md"],
    "Исправлены битые относительные ссылки на разделы руководства.",
    """--- a/docs/api-reference.md
+++ b/docs/api-reference.md
@@ -77,7 +77,7 @@ See the
-[guide](../guides/inference.md)
+[guide](../guide/inference.md)
""",
    "none", [], None, None, [], [],
    "en", ["negative", "noise"], "medium",
    "Правка ссылок внутри документации",
    None,
    "Пара DOCS_ONLY кейсов нужна, чтобы оценить долю PR, которые агент обязан пропустить целиком, но отметить в отчёте Eval как 'docs-only'.",
)

# ---------------------------------------------------------------- bootstrap
BOOTSTRAP_STAGE1_RU = case(
    "gold-042", "bootstrap", "escalate_bootstrap", ["new_user_feature"],
    ["demopkg/__init__.py", "demopkg/client.py", "demopkg/exporters.py", "demopkg/settings.py"],
    "Первый запуск: в репозитории нет папки docs/, README существует только в зачаточном виде.",
    """# bootstrap scan (not a diff)
packages: demopkg (4 modules, 11 public symbols)
public_symbols: APIClient, Client, Settings, export_to_csv, export_to_parquet,
                iter_export_chunks, ImageClassifier, Session, RetryPolicy,
                StorageBackend, create_backend
docs_dir: MISSING
readme: 6 lines, no installation section
entry_points: none
dependencies: pandas>=2.0, openai>=1.40, diskcache>=5.6
""",
    "add", ["docs/index.md", "docs/api-reference.md", "docs/guide/getting-started.md"], "Stage 1: scaffold",
    """# Документация demopkg

* [Начало работы](guide/getting-started.md) — установка и первый запрос
* [Справочник API](api-reference.md) — классы и функции пакета
* [Архитектурные решения](adr/) — почему всё устроено так

## Что делает пакет

`demopkg` — клиент HTTP API с выгрузкой результатов в CSV и Parquet,
файловым кэшем и локальным инференсом моделей ONNX.
""",
    ["docs/index.md", "api-reference.md", "getting-started.md", "demopkg"],
    ["полное описание каждой функции", "ADR 0001"],
    "ru", ["positive", "hard_for_small_model"], "high",
    "Bootstrap Stage 1 — структура docs/ и index",
    None,
    "Bootstrap разбивается на 4 stage (см. §5 плана). Здесь ожидается ТОЛЬКО каркас: index + скелеты разделов. Ловушка для модели — соблазн сгенерировать сразу весь API-ref.",
)

BOOTSTRAP_STAGE3_EN = case(
    "gold-043", "bootstrap", "escalate_bootstrap", ["api_new"],
    ["demopkg/exporters.py"],
    "Bootstrap Stage 3: чанк API reference по одному модулю (exporters), 3 публичных символа.",
    """# bootstrap scan (not a diff)
module: demopkg.exporters
public_symbols:
  export_to_csv(df, path, sep=",")
  export_to_parquet(df, path, compression="snappy")
  iter_export_chunks(df, chunksize=10_000)
docstrings: present for all 3
existing_section: none
""",
    "add", ["docs/api-reference.md"], "Module demopkg.exporters",
    """## `demopkg.exporters`

| Function | Signature | Purpose |
|---|---|---|
| `export_to_csv` | `(df, path, sep=",")` | Write a DataFrame as delimited text. |
| `export_to_parquet` | `(df, path, compression="snappy")` | Write a DataFrame as Parquet. |
| `iter_export_chunks` | `(df, chunksize=10_000)` | Yield row chunks for streaming export. |

All exporters accept a `pandas.DataFrame` and write to `path`; they return
`None` except `iter_export_chunks`, which is a generator.
""",
    ["demopkg.exporters", "export_to_csv", "export_to_parquet", "iter_export_chunks"],
    ["export_to_json", "export_to_excel", "engine='fastparquet'"],
    "en", ["positive", "hard_for_small_model"], "high",
    "Bootstrap Stage 3 — API reference по чанку из одного модуля",
    None,
    "Ключевой кейс против галлюцинаций: must_not_include содержит функции, которых в модуле нет, но которые модель часто додумывает по аналогии.",
)

BOOTSTRAP_ADR_SKELETON_RU = case(
    "gold-044", "bootstrap", "escalate_bootstrap", ["adr_new"],
    ["demopkg/storage/", "demopkg/llm/backend.py", "pyproject.toml"],
    "Bootstrap Stage 4: по структуре пакетов определены 3 кандидата на ADR, скелеты создаются без выдуманных решений.",
    """# bootstrap scan (not a diff)
adr_candidates:
  1. storage abstraction (demopkg/storage/base.py, abc + factory)
  2. llm backend via OpenAI-compatible client (demopkg/llm/backend.py)
  3. checkpoint storage engine (demopkg/state/store.py, sqlite3)
decision_evidence: code structure only, no commit messages available
""",
    "add", ["docs/adr/README.md", "docs/adr/0001-template.md"], "Скелеты ADR",
    """# Архитектурные решения (ADR)

Шаблон записи: **Контекст / Решение / Последствия**. Статусы: `предложен`,
`принят`, `изменён NNNN`, `отклонён`.

Кандидаты на заполнение (по структуре кода, решения не подтверждены):

| Номер | Тема | Признак в коде |
|---|---|---|
| 0002 | Абстракция слоя хранения | `StorageBackend` + фабрика `create_backend` |
| 0003 | LLM-бэкенд через OpenAI-совместимый API | `OPENAI_BASE_URL`, отсутствие прямой зависимости от бэкенда |
| 0004 | Движок чекпоинтов состояния | `sqlite3`, интерфейс `load_state` |

Текст решений заполняется вручную или агентом по мере появления соответствующих PR.
""",
    ["Контекст", "Решение", "Последствия", "Кандидаты"],
    ["потому что команда выбрала", "мотивация отказа от"],
    "ru", ["positive", "hard_for_small_model"], "medium",
    "Bootstrap Stage 4 — скелеты ADR по кандидатам",
    None,
    "Самый опасный bootstrap-кейс: модель обязана НЕ выдумывать мотивацию решений. must_not_include запрещает причинные формулировки — детектируется детерминированным матчером.",
)

BOOTSTRAP_STAGE2_README_RU = case(
    "gold-040", "bootstrap", "escalate_bootstrap", ["dependency_added"],
    ["README.md", "pyproject.toml", "demopkg/settings.py"],
    "Bootstrap Stage 2: каркас docs/ уже создан (Stage 1 завершён); теперь по коду и pyproject заполняется фактическая часть README — установка и quickstart.",
    """# bootstrap scan (not a diff)
state: docs/index.md EXISTS (stage 1 done), docs/api-reference.md is scaffold only
readme_current: "# demopkg\\n\\nКлиент API и выгрузка данных.\\n" (4 lines)
pyproject: requires-python >=3.10; deps pandas>=2.0, openai>=1.40, diskcache>=5.6
env_vars_in_settings: DEMOPKG_API_TOKEN (required), DEMOPKG_BASE_URL (optional, default https://api.example.com)
entry_points: console_scripts demopkg = demopkg.cli:main
""",
    "update", ["README.md"], "Установка и быстрый старт",
    """## Установка

```bash
pip install git+https://github.com/demo-org/demopkg.git
```

Требуется Python 3.10+. Зависимости: pandas, openai, diskcache.

## Быстрый старт

```python
from demopkg import APIClient

client = APIClient(token=os.environ["DEMOPKG_API_TOKEN"])
records = client.fetch_records("exports", limit=10)
```

Переменная окружения `DEMOPKG_API_TOKEN` обязательна; `DEMOPKG_BASE_URL`
необязательна (по умолчанию `https://api.example.com`).
""",
    ["pip install", "DEMOPKG_API_TOKEN", "fetch_records", "Python 3.10"],
    ["brew install", "conda install demopkg", "docker run demopkg", "DEMOPKG_API_KEY"],
    "ru", ["positive", "hard_for_small_model"], "high",
    "Bootstrap Stage 2 — фактическая часть README из кода и pyproject",
    None,
    "Вторая ловушка против галлюцинаций: модель обязана взять версии зависимостей и имена env-переменных СТРОГО из скана, а не из типовых шаблонов README. must_not_include запрещает альтернативные способы установки и выдуманное имя DEMOPKG_API_KEY (в коде — DEMOPKG_API_TOKEN). Stage выполняется только после одобренного Stage 1.",
)

BOOTSTRAP_STAGE5_CHANGELOG_EN = case(
    "gold-041", "bootstrap", "escalate_bootstrap", ["behavior_changed"],
    ["docs/changelog.md", "git_log: last 15 commits"],
    "Bootstrap Stage 5: история изменений восстанавливается по git log; агент не знает дат релизов и оформляет их как кандидаты на подтверждение лидом.",
    """# bootstrap scan (not a diff)
git_log_subjects (oldest first):
  add APIClient with fetch_records
  support timeout parameter in fetch_records
  add export_to_parquet (snappy by default)
  fix crash on empty DataFrame in export_to_csv
  introduce StorageBackend abstraction
  add retry policy with exponential backoff
unreleased_commits: 2 (storage abstraction, retry policy)
tags: none
""",
    "add", ["docs/changelog.md"], "Unreleased + ретроспектива",
    """# Changelog

All notable changes to `demopkg` are documented here.
Format loosely follows Keep a Changelog; versions were not tagged in git,
so historical grouping below is **proposed** and needs maintainer approval.

## [Unreleased]

### Added
- `StorageBackend` abstraction with pluggable implementations.
- Retry policy with exponential backoff for transient API failures.

## Proposed 0.3.0
- Added `timeout` parameter to `fetch_records`.
- Added `export_to_parquet` (Snappy compression by default).

## Proposed 0.2.0
- Fixed crash on empty DataFrame in `export_to_csv`.

## Proposed 0.1.0
- Initial `APIClient` with `fetch_records`.
""",
    ["[Unreleased]", "Proposed", "export_to_parquet", "StorageBackend"],
    ["2024-", "2025-", "2026-", "Breaking release 1.0"],
    "en", ["positive", "edge", "hard_for_small_model"], "medium",
    "Bootstrap Stage 5 — changelog по git log",
    None,
    "Последний stage (после 3 и 4, когда есть docs/). Ключевое ограничение: тегов в git нет, поэтому агент помечает группы как 'Proposed' и никогда не ставит даты/номера релизов — must_not_include ловит год-паттерн и выдуманный мажорный релиз. Это проверка того, что bootstrap умеет честно заявлять неуверенность вместо уверенной галлюцинации.",
)

CASES = [
    API_NEW_RU, API_CHANGED_EN, API_REMOVED_RU, API_DEFAULT_CHANGED_EN, API_MULTI_FILE_RU,
    CONFIG_ENV_CHANGED_EN, FEATURE_TFLITE_EXAMPLES_RU, FEATURE_BENCH_GUIDE_EN,
    FEATURE_USER_WORKFLOW_RU, FEATURE_EXPORT_STREAMING_EN, ADR_NEW_BACKEND_RU,
    ADR_MODEL_SWITCH_EN, ADR_DB_CHOICE_RU, ADR_DEPS_AUDIT_EN, README_INSTALL_RU,
    README_QUICKSTART_EN, README_BADGES_PY311_EN, README_CONTRIB_SETUP_RU,
    README_CHANGELOG_SECTION_EN, README_VERSION_PIN_EN, TYPO_README_RU,
    TYPO_DOCSTRING_EN, TYPO_DOCS_PROSE_RU, TYPO_VARIABLE_NAMING_EN,
    REFACTOR_PRIVATE_EN, REFACTIR_TYPE_HINTS_RU, REFACTOR_MOVE_MODULE_EN,
    REFACTOR_TEST_ONLY_RU, REFACTOR_PERF_INTERNAL_EN, CI_WORKFLOW_MATRIX_EN,
    CI_ADD_COVERAGE_RU, CI_FIX_FLAKY_TEST_EN, CI_PRECOMMIT_CONFIG_RU,
    STYLE_BLACK_DIFF_EN, STYLE_LINT_FIX_RU, STYLE_DOCSTRING_REFORMAT_EN,
    STYLE_RENAME_LOCAL_VAR_EN,
    DOCS_ONLY_NEW_SECTION_RU, DOCS_ONLY_LINK_FIX_EN, BOOTSTRAP_STAGE1_RU,
    BOOTSTRAP_STAGE2_README_RU, BOOTSTRAP_STAGE5_CHANGELOG_EN,
    BOOTSTRAP_STAGE3_EN, BOOTSTRAP_ADR_SKELETON_RU,
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate gold dataset v1")
    ap.add_argument("--out", default="data/gold/cases", help="куда писать кейсы")
    ap.add_argument("--force", action="store_true", help="перезаписать существующие")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = [c["id"] for c in CASES]
    if len(set(ids)) != len(ids):
        sys.exit("Дубликаты id в определении кейсов!")

    written = skipped = 0
    for c in CASES:
        path = out_dir / f"{c['id']}.json"
        if path.exists() and not args.force:
            skipped += 1
            continue
        path.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1

    neg = sum(1 for c in CASES if c["expected_behavior"] == "stay_silent")
    print(f"Записано: {written}, пропущено (уже есть): {skipped}, всего кейсов: {len(CASES)}")
    print(f"Negative: {neg} ({neg / len(CASES):.0%})")
    print(f"Каталог: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
