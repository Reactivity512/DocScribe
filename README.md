# DocScribe / DocAgent

Агент автодокументации с HITL-ревью через GitHub PR: смотрит в diff pull request,
по детерминированным правилам решает, нужна ли документация, генерирует черновик
(README / api-reference / guide / ADR / changelog) и открывает PR. Дальше решает
team lead: **мержит** или **пишет комментарий** — на комментарий бот отвечает
(правит тот же PR либо объясняет решение).

## Что уже сделано (шаги 0-6)

| Шаг | Состояние | Где смотреть |
|---|---|---|
| 0. Baseline + gold dataset | готово: 54 кейса, схема `gold-v1` | [data/gold](data/gold), [план](docs/plan/STEP0_baseline_gold_dataset.md) |
| 1. MCP-сервер | готово: 7 инструментов, 2 ресурса, prompt | [server.py](src/docagent/mcp_server/server.py) |
| 2. LLM-агент | готово: fake/ollama/vllm, гард от галлюцинаций | [loop.py](src/docagent/agent/loop.py) |
| 3. LangGraph-оркестратор | готово: SqliteSaver, `interrupt()`, цикл правок | [graph.py](src/docagent/orchestrator/graph.py) |
| 4. GitHub-интеграция | готово: diff и метаданные PR тянет нода `fetch_pr`, публикация PR, обновление того же PR | [github_int](src/docagent/github_int) |
| 5. HITL-цикл | готово: polling комментариев, правка/объяснение, resume графа | [watcher.py](src/docagent/github_int/watcher.py), [review.py](src/docagent/github_int/review.py) |
| 6. Eval | готово: gold-регрессия (гейт) + онлайн-метрики приёмки | [eval](src/docagent/eval), [план](docs/plan/STEP4-6_github_hitl_eval.md) |

Подробный план шагов 4-6 и принятые решения — [docs/plan/STEP4-6_github_hitl_eval.md](docs/plan/STEP4-6_github_hitl_eval.md).

## Ключевые решения

1. **Бот не мержит.** PR публикуется как обычный (не draft), решение «готово»
   выражается мержем лида. Draft используется только как исключение — для
   эскалации `needs_human_edit` (лимит автоправок исчерпан).
2. **Комментарий человека — сигнал.** Разбирается детерминированно (ru/en):
   * «добавь/поправь/please add» → правка того же PR + подтверждение в треде;
   * «неверно/incorrect» → возражение: бот объясняет, правит по явной просьбе;
   * «почему/why…?» → ответ в треде без переписывания;
   * непонятное → уточняющий вопрос (комментарий без реакции невозможен).
3. **PR создаётся до ожидания ревью** (для `DOCAGENT_PUBLISH_TARGET=github`):
   лид ревьюит diff самого PR. Для `outbox` порядок прежний — interrupt до записи.
4. **Источник событий — polling**, без вебхука: демо-репозиторий локальный, а тот
   же `process_once` потом вызывается из вебхук-обработчика без изменений.
5. **Что документировать — решает бот** по правилам шага 1 (триггеры + целевые
   документы). Лид ничего не заказывает заранее.

## Быстрый старт

Нужен Python 3.10+ и токен GitHub с правом создавать PR в целевом репозитории.

```powershell
cd C:\Users\Greybyte\DocScribe
pip install -e ".[dev]"          # или: pip install -r requirements.txt

$env:GITHUB_TOKEN = "github_pat_..."   # НЕ коммитить; .env в .gitignore
$env:PYTHONUTF8 = 1
$env:PYTHONPATH = "src"
```

Дальше три способа: **локально без сети** (быстрая проверка), **полный цикл на
GitHub** (демо-репозиторий) и **оценка качества** (шаг 6).

## Сценарий 1 — локально, без GitHub (правила + агент + HITL в терминале)

```powershell
# 1. Прогон gold-кейса насквозь до публикации в data/outbox/
python -m docagent.orchestrator.cli --gold gold-005 --backend fake --auto-approve

# 2. Интерактивное HITL-ревью в консоли (решение вводится руками)
python -m docagent.orchestrator.cli --gold gold-005 --backend fake

# 3. Продолжить ранее прерванный тред (например, после закрытия терминала)
python -m docagent.orchestrator.cli --resume --thread pr-gold-005 --decision changes --feedback "добавь пример"
```

Результат: `data/outbox/<thread>.json` с payload (файлы, тело PR, commit message)
и решениями ревьюера.

## Сценарий 2 — полный цикл на GitHub (демо-репозиторий)

```powershell
$env:DOCAGENT_PUBLISH_TARGET = "github"

# 0. Проверить токен и доступ
python -m docagent.github_int.cli whoami

# 1. Наполнить демо-репо демо-пакетом (идемпотентно; сначала --dry-run)
python -m docagent.github_int.cli bootstrap --repo Reactivity512/python-demo-repo --dry-run
python -m docagent.github_int.cli bootstrap --repo Reactivity512/python-demo-repo

# 2. Создать тестовый PR из gold-кейса (ветка test/<case> от main)
python -m docagent.github_int.cli seed-pr --repo Reactivity512/python-demo-repo --case gold-005

# 3. Разбор PR: diff + метаданные + решение правил (без LLM)
python -m docagent.github_int.cli diff --repo Reactivity512/python-demo-repo --pr 1 --no-clone

# 4. Полный цикл: бот открывает PR с документацией
#    (diff и метаданные тянет нода fetch_pr; thread_id = pr-<owner>-<repo>-<номер>,
#     ветка бота = docagent/<thread_id>)
python -m docagent.github_int.cli run --repo Reactivity512/python-demo-repo --pr 1 --backend fake

# 5. HITL: ждать комментариев лида и реагировать на них
python -m docagent.github_int.cli watch --thread pr-Reactivity512-python-demo-repo-1 --interval 15
#    один проход без ожидания:  ... watch --thread <тот же> --once
#    только показать план:      ... watch --thread <тот же> --once --dry-run
```

Что делает лид в GitHub:

| Действие лида | Реакция бота |
|---|---|
| Комментарий «добавь пример для list_items» | правит тот же PR, отвечает «обновил черновик» |
| Комментарий «это неверно» | отвечает разбором, правит по явной просьбе |
| Комментарий «почему затронут api-reference?» | отвечает в треде, PR не трогает |
| Мерж PR | тред закрывается, watcher завершает работу |
| Закрытие PR без мержа | тред закрывается как отклонённый |

## Сценарий 3 — оценка (шаг 6)

```powershell
# gold-регрессия: правила + агент, пороги-гейт, отчёты в data/reports/
python -m docagent.github_int.cli eval --what gold --backend fake
#   PASS -> exit 0, FAIL -> exit 1 (годится для CI)

# только правила (мгновенно) / быстрая проверка на подмножестве
python -m docagent.github_int.cli eval --what gold --limit 5

# онлайн-метрики приёмки: merged_rate, правки до мержа, ожидающие ревью
python -m docagent.github_int.cli eval --what online

# всё сразу
python -m docagent.github_int.cli eval
```

Отчёты: `data/reports/eval_gold.{json,md}`, `data/reports/eval_online.{json,md}`.
Текущие пороги гейта — в [gold.py](src/docagent/eval/gold.py) (`Gates`):
behavior accuracy ≥ 0.85, FP на negative ≤ 0.05, drafts ≥ 0.6, target match ≥ 0.8.
Baseline правил — accuracy 0.8889 при FP 0.0 (target/trigger coverage намеренно
не гейтятся: известные ограничения шага 1, см. отчёт).

## MCP-сервер отдельно

```powershell
python -m docagent.mcp_server.server              # stdio (для MCP-клиента)
python -m docagent.mcp_server.server --http 8765  # streamable HTTP
```

Инструменты: `get_pull_request_context`, `analyze_pr_diff`, `should_update_docs`,
`get_public_api`, `diff_public_api`, `read_file`, `search_codebase`,
`plan_bootstrap`, `verify_api_change`. Ресурсы: `config://docagent`,
`rules://triggers`. Промпт: `review_pr`.

> Ограничение MVP: агент вызывает тот же сервисный слой напрямую
> ([loop.py](src/docagent/agent/loop.py), `ToolProvider`), а не по MCP-протоколу —
> MCP-сервер работает как отдельный интерфейс для внешних клиентов.

## Конфигурация (env, префикс `DOCAGENT_`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DOCAGENT_DOCS_BACKEND` | `ollama` | `fake` \| `ollama` \| `vllm` |
| `DOCAGENT_MODEL` | `qwen2.5-coder:3b` | модель для генерации |
| `DOCAGENT_OPENAI_BASE_URL` | `http://localhost:11434/v1` | OpenAI-совместимый эндпоинт |
| `DOCAGENT_LLM_MAX_TOKENS` | `900` | лимит ответа (мало → обрезанный JSON) |
| `DOCAGENT_DOCS_LANG` | `ru` | язык документации: `ru` \| `en` |
| `DOCAGENT_PUBLISH_TARGET` | `outbox` | `outbox` \| `github` |
| `DOCAGENT_DEMO_REPO` | `Reactivity512/python-demo-repo` | репозиторий по умолчанию |
| `DOCAGENT_BOT_BRANCH_PREFIX` | `docagent/` | префикс веток бота |
| `DOCAGENT_CHECKPOINT_URI` | пусто | пусто → Sqlite `data/checkpoints.db`; `postgres://…` → прод |
| `DOCAGENT_WATCH_INTERVAL_S` | `15` | интервал polling в `watch` |
| `GITHUB_TOKEN` | — | PAT (или `DOCAGENT_GITHUB_TOKEN`) |

## Артефакты во время работы

* `data/checkpoints.db` — состояние тредов (можно убить процесс и продолжить);
* `data/outbox/<thread>.json` — публикации в режиме outbox;
* `data/work/<thread>.watch.json` — обработанные комментарии (идемпотентность watcher);
* `data/logs/runs.jsonl` — события HITL-нод (аудит решений ревьюера);
* `data/reports/` — отчёты eval.

## Тесты

```powershell
python -m pytest tests -q              # 107 тестов: MCP-правила, агент, граф, github (HTTP-мок), HITL, eval
python tests/gold_harness/run_gold.py  # метрики правил на gold-сете (шаг 1)
python tests/agent/run_agent_gold.py --backend fake   # метрики агента (шаг 2)
```

Клиент GitHub покрыт тестами против mock-GitHub по настоящему HTTP
([test_client_http.py](tests/github_int/test_client_http.py)): проверяются тела
запросов, создание/сброс ветки, блобы, обычный и draft PR, комментарии и review.
Это ловит класс ошибок, невидимый при подмене клиента фейком (именно так был
найден сломанный `diff_text`).

## Известные ограничения

* **Вебхука нет** — только polling (`watch`); вебхук подключается тем же
  `process_once` без переписывания логики.
* **Классификация комментариев детерминированная** (правила ru/en). LLM-уточнение
  намерения не подключено: цена ошибки асимметрична, лишняя переписка хуже вопроса.
* **Качество прозы 3B-модели** ограничено: известные target-mismatch кейсы шага 2
  (gold-003/004/006/012/013/048/050) — см. [отчёт](data/reports/step2_agent_ollama_v2.md).
* **Правки генерируются заново по всем целевым документам** кейса, а не точечно по
  абзацу: контракт `revise` в [nodes.py](src/docagent/orchestrator/nodes.py).

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| `AssertionError` в `create_git_tree` | PyGithub ≥ 2.x требует `InputGitTreeElement` (исправлено в [client.py](src/docagent/github_int/client.py)) |
| `No commits between main and <branch>` | ветка бота идентична базе: закройте старый PR / удалите ветку `docagent/<thread>` |
| `A pull request already exists for …` | PR по ветке уже открыт — бот обновляет его; при легаси-PR закройте старый |
| `publish skipped: nothing to commit …` | черновик совпадает с уже смерженным в main — правьте документ или меняйте кейс |
| `fetch_pr: GitHub недоступен …` | нет сети/токена: граф не падает, пустой diff → `silent`, причина в `errors` |
| `watch`: «нет состояния графа для thread» | сначала `run` на PR, затем `watch` с тем же `--thread` |
| `diff_text` 404 на любом PR | было в ранних сборках (URL `/repos///r/…`); обновите код — теперь diff идёт прямым HTTP-запросом |
