# Шаги 4-6 — доработка до завершённого MVP

Дополняет `STEP0_baseline_gold_dataset.md` и `STEP3_langgraph_orchestrator.md`.
Фиксирует решения, принятые после разбора кода: шаги 0-3 закрыты, шаг 4 закрыт
наполовину (write-side есть, `fetch_pr` — заглушка), шаги 5-6 не начаты.

## Принятые решения (со слов заказчика)

1. **Бот не мержит.** PR публикуется как обычный (не draft) и висит, пока лид его
   не смержит руками. Мерж лида = сигнал «готово»; после мержа тред закрывается.
2. **Комментарий человека = сигнал.** «Если кто-то из разработчиков написал, что
   внесения неверные, бот проанализировал и исправил или объяснил, почему так лучше».
   Отсюда два исхода на комментарий: правка PR либо пояснение в треде.
3. **Правка — только по явной просьбе изменить.** Комментарий-вопрос получает ответ
   в треде и НЕ запускает цикл переписывания.
4. **Источник событий — polling** (вебхук не нужен: python-demo-repo создан для
   демонстрации MVP; при выстреле проекта — вебхук тем же обработчиком).
5. **Eval — минимум**: gold-регрессия одним отчётом-гейтом + онлайн-метрики приёмки.
6. **Языки ru/en** уже поддержаны (`DOCAGENT_DOCS_LANG`, промпты) — сохраняем.
7. **Что документировать — решает бот** по детерминированным триггерам шага 1
   (api_new/api_signature_changed/api_removed/behavior_changed/dependency_added/
   config_or_env_changed/adr_new/adr_changed/breaking_change/new_user_feature) и
   expected_docs_targets (readme | api-reference | adr | guide | changelog). Лид
   ничего не заказывает заранее — он видит готовый PR.

## Изменение схемы графа (следствие решений 1-3)

```
сейчас:  prepare_payload -> human_approval(interrupt) -> publish(создать draft PR)
надо:    prepare_payload -> publish(создать/обновить PR) -> human_approval(interrupt)
                                 │                                  │
                                 │        comment(changes) -> revise ┘
                                 │        comment(question) -> reply, остаёмся ждать
                                 │        merged -> finalize_merged
                                 │        closed -> finalize_rejected
```

Порядок ветвится по `DOCAGENT_PUBLISH_TARGET`:
* `outbox` (тесты/CLI) — как сейчас: interrupt ДО записи, ревьюить нечего;
* `github` — PR создаётся ДО ожидания: лиду нужно что-то смотреть в diff.

Инвариант «единственная side-effect нода — publish» сохраняется: `revise` лишь
пересобирает payload, публикация/обновление PR по-прежнему в `publish`.

## Шаг 4 — GitHub: чтение внутрь графа

- [ ] `client_of(state)` / `pr_slug(state)` — общий резолв клиента, slug и номера PR
      из `pr_ref` (URL вида `.../pull/N`, `owner/repo#N`, локальный `.diff`);
      `_repo_slug` из publisher.py переиспользуется, а не дублируется.
- [ ] `fetch_pr` — реальное чтение: `GitHubClient.diff_text()` (REST, Accept: diff)
      при `publish_target=github`; для локального `.diff`/`--gold` остаётся текущий путь.
- [ ] Комментарий/описание PR кладём в state (`pr_title`, `pr_body`) — нужны промпту
      и телу публикуемого PR.
- [ ] `--pr-ref` перестаёт быть обязательным костылём: CLI `run` передаёт ссылку,
      узел сам достаёт diff (совместимость с `--diff` сохраняется).
- [ ] Идемпотентность: повторный прогон того же thread → тот же PR (обновление
      ветки/тела), без 422 «A pull request already exists».
- [ ] `seed-pr` — генерация тестовых PR в python-demo-repo из gold-кейсов
      (ветка `test/<case>` от main, обычный PR) для проверки 5-го шага вживую.

## Шаг 5 — HITL-цикл

- [ ] `watcher` (polling, `DOCAGENT_WATCH_INTERVAL_S`, по умолчанию 15):
      `repo.get_pulls(state="open", head="<owner>:<branch>")` → комментарии
      `pr.get_issue_comments()` → фильтр «не бот» (по `login` из `whoami()`).
- [ ] Дедупликация: множество обработанных `comment.id` в чекпоинте (состояние
      графа) — перезапуск `watch` не приводит к повторному resume.
- [ ] Классификация намерения комментария (детерминированные правила + LLM-уточнение):
      * `changes` — просьба поправить/добавить/переписать → `revise`;
      * `question` — «почему так?», «откуда вывод?» → ответ в треде, граф не будим;
      * `objection` — «это неверно» → сначала пояснение, при подтверждении просьбы —
        правка (решение 2).
- [ ] Ответ бота в тред: «обновил черновик: <файлы>, посмотрите снова» /
      пояснение по решению в 1-2 абзацах. Комментарий без реакции невозможен.
- [ ] `resume` через `Command(resume={"decision": "changes", "feedback": ...})`
      поверх уже существующего thread + обновление того же PR (шаг 4).
- [ ] Терминальные состояния: PR смержен → `finalize_merged` (тред закрыт);
      PR закрыт без мержа → `finalize_rejected`; лимит правок исчерпан →
      `needs_human_edit` (PR остаётся **draft** как исключение из решения 1).
- [ ] Запуск: `python -m docagent.github_int.cli watch --repo ... --thread ...`.
- [ ] Дозаполнить DoD шага 3: события нод писать в `data/logs/runs.jsonl`
      (`Settings.runs_log` объявлен, но нигде не используется — события сейчас
      живут только в состоянии графа).

## Шаг 6 — Eval (минимум)

- [ ] Gold-регрессия одним отчётом-гейтом: правила (`run_gold.py`) + агент
      (`run_agent_gold.py`) → `data/reports/eval_gold.json/md` с порогами
      (behavior accuracy, FP-rate на negative, target coverage, drafts_produced).
- [ ] Онлайн-метрики приёмки: сколько PR дошли до мержа, сколько правок до мержа,
      время PR→ready/merged, причины отклонений из комментариев, FP-rate (PR на
      negative-кейсах, где агент должен был молчать).
- [ ] Команда отчёта: `python -m docagent.github_int.cli eval [--gold] [--online]`.

## DoD всего проекта

1. `seed-pr` наполняет демо-репо PR-ами; `run` создаёт по ним PR с документацией.
2. Комментарий человека в PR-е запускает правку или пояснение без ручного CLI.
3. Мерж лида закрывает тред; бот не создаёт дублей PR.
4. `eval` печатает gold-гейт и онлайн-метрики; README описывает полный цикл.

## Что сделано (факт)

Шаги 4-6 реализованы, покрытие: **107 тестов**, `pytest tests -q` зелёный.

* Шаг 4: `refs.py` (единый разбор ссылок и резолв клиента), `fetch_pr` тянет diff и
  метаданные PR, `pr_slug/pr_number/pr_title/pr_body/pr_base/pr_head` в состоянии,
  публикация в тот же PR без дублей, `seed-pr` для тестовых PR.
* Шаг 5: `client` получил `find_pr/bot_login/list_comments/comment/review_ready`;
  `review.py` — детерминированная классификация (ru/en) с разбором по клаузам;
  `watcher.py` — polling, дедупликация по `comment.id`, resume графа, CLI `watch`.
  Порядок графа для github: `prepare_payload -> publish_first -> human_approval`
  (PR создаётся до ожидания ревью), правка по ревью идёт через `revise -> publish`
  и завершается `finalize_published`.
* Шаг 6: `cli eval --what gold|online|all`; gold-гейт переиспользует харнесы шагов
  1-2; онлайн-метрики читают `checkpoints.db` (msgpack через штатный сериализатор
  LangGraph), `*.watch.json` и outbox. Текущий прогон: PASS (accuracy 0.8889,
  FP 0.0, drafts 0.7778, target match 1.0).

### Починка gold-сета и скрытый баг анализатора

Живой прогон `seed-pr` вскрыл, что **ни один** diff gold-сета не применяется
`git apply`:
  * нет заголовков `diff --git a/… b/…` (сразу `--- a/…`), из-за чего второй файл
    патча считается мусором — «corrupt patch at line N»;
  * счётчики hunk-заголовков не соответствуют телу в 45 кейсах из 49.

Добавлены `gitpatch.normalize_patch` (заголовки + пересчёт счётчиков, аналог
`git apply --recount`) и `repair_patch` (пересобирает и уже существующие
заголовки); [scripts/fix_gold_patches.py](scripts/fix_gold_patches.py) починил 48
кейсов на месте. Тело diff'ов не изменилось.

После починки всплыл баг анализатора: в `text_signals.detect_config_env` стояло
`value.strip(' "', "'")` — два аргумента у `str.strip`, то есть `TypeError`.
Ветка не выполнялась, пока парсер не видел удалённые строки конфигов. Исправлено
хелпером `_unquote`. Побочный эффект: распознавание улучшилось —
behavior accuracy выросла с 0.8704 до **0.8889** (перестал теряться gold-014).

### Баги, найденные при верификации (важно для истории)

1. **`diff_text` ломался всегда**: `owner, name = slug.partition("/")` давало
   `owner="o/r"`, `name=""` -> URL `/repos///r/pulls/N` (404 на каждом `fetch_pr`).
   Найдено тестом против mock-GitHub; переписано на прямой HTTP через urllib
   (PyGithub `requestJson` вдобавок отдавал тело то строкой, то dict и повторял
   запрос при сбое чтения, из-за чего вместо diff-а приходил JSON ошибки).
2. **Цикл графа** `publish -> human_approval -> publish`: при повторном входе в
   ноду LangGraph терял выставленные ею признаки (`review_pending`,
   `publish_target`), поэтому граф либо крутился бесконечно (260+ событий
   `human_approval`), либо уходил в END. Исправлено разделением публикации на две
   ноды: цикл убран по построению.
3. **Ключи вне схемы состояния отбрасываются** LangGraph: `updated`,
   `publish_target`, `pr_url` не сохранялись, пока не добавлены в `OrchState`.
4. `_looks_like_path` считал `https://…` локальным путём (двоеточие схемы), regex
   слага использовал `\w` (не матчит `/`), `watcher` передавал ссылку `slug#N`,
   которую разбор не понимал.
5. Мелочи: `Github(tok)` -> `Auth.Token` (DeprecationWarning), LLM-таймаут для
   HTTP-diff, статус `pr_exists` для повторной публикации.
6. **`cmd_whoami` падал**: `dict(c.api.get_rate_limit())` — `RateLimitOverview` не
   итерируется (`TypeError`). Теперь поля берутся явно.
7. **`seed-pr` писал патч не туда**: путь строился как `wd.parent.parent`, то есть
   каталог НАД репозиторием (`C:\Users\Greybyte\data\work`). Исправлено на корень
   проекта; добавлен фолбэк `git apply --recount`.

### Ограничение проверки

Живой прогон на `Reactivity512/python-demo-repo` из среды разработки невозможен:
сеть закрыта полностью (`api.github.com`, `example.com`, `pypi` — `000`). Вместо
этого клиент проверен на mock-GitHub по настоящему HTTP (14 тестов: тела запросов,
ветки, блобы, draft/ready, комментарии, review). Команды для живого прогона —
в README, раздел «Сценарий 2».
