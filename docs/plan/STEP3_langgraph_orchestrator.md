# Шаг 3 — LangGraph Orchestrator

## Цель
Собрать end-to-end конвейер «PR → анализ → черновики → (шаг 4) draft PR → (шаг 5) HITL»
как граф LangGraph **с checkpointing и interrupt-точками с самого начала** (решение из шага 0,
чтобы шаг 5 не стал рефакторингом шага 3).

## Граф
```
fetch_pr -> analyze_diff -> [route]
   ├─ no_trigger -> finalize_silent (END)          # метрика: silent-skip
   └─ trigger    -> generate_drafts                # агент (шаг 2), retry до 2 попыток на target
                   -> self_check                  # детерминированные проверки качества
                   -> prepare_payload             # файлы+коммит-сообщение+тело PR (для шага 4)
                   -> human_approval              # interrupt() — здесь шагов 4/5 вставляет GitHub
                   ├─ approved   -> publish       # (шаг 4 stub: запись payload в data/outbox/)
                   ├─ changes    -> revise        # правки по замечаниям -> снова self_check (loop, max 3)
                   └─ rejected   -> finalize_rejected (END)
```

## Состояние (DocAgentState, TypedDict)
pr_ref, diff, api_snapshot, triggers[], drafts[], drop_reasons[], revisions_count,
approval: {decision, feedback}, outbox_path, lang(en|ru), status, errors[]

## Ключевые решения дизайна
1. **Checkpointer:** MVP — `SqliteSaver` (файл `data/checkpoints.db`); прод-путь — Postgres (`langgraph-checkpoint-postgres`), переключение через env `DOCAGENT_CHECKPOINT_URI`. Один интерфейс — две реализации.
2. **Interrupt:** `interrupt()` из langgraph перед `publish`; resume через `Command(resume={"decision": ..., "feedback": ...})`. В шаге 3 источник решений — CLI-стаб / автотесты; в шаге 5 его заменит webhook-обработчик комментариев GitHub, точка прерывания не изменится.
3. **Retry-политика:** генерация target'а повторяется максимум 2 раза при дропе гардом (backoff не нужен, локально); revise-loop после замечаний — максимум 3 цикла, затем эскалация: статус `needs_human_edit`, PR публикуется как есть с пометкой.
4. **Side-effect boundary:** единственная нода с внешними записями — `publish`. Всё остальное идемпотентно и переносимо между бэкендами. Это даёт чистый seam для шага 4.
5. **Наблюдаемость:** каждая нода пишет structured event (jsonl `data/logs/runs.jsonl`): node, pr, duration_ms, counts. Задел под аудит (решение 7 — основной аудит в PR-треде, но события у нас).

## DoD шага 3
- [ ] Граф собирается, `langgraph` в requirements; unit: маршрутизация trigger/no-trigger, retry, revise-loop, лимиты.
- [ ] e2e-тест на gold-кейсах (backend=fake): 3 write-кейса проходят полный путь до outbox, negative-кейс уходит в silent END.
- [ ] Interrupt/resume работает поверх SqliteSaver: процесс можно убить и продолжить (тест с новым экземпляром графа и тем же thread_id).
- [ ] Метрики прогона пишутся в отчёт (drafts_produced, dropped, revisions_avg, wall_time).
- [ ] README-секция «Как запустить оркестратор локально».

## Оценка текущих метрик шага 2 (перед шагом 3)
drafts_produced_rate=0.95, hallucination_free=0.526 (ложные срабатывания гарда, решено оставить),
target_match=1.0, must_include_hit=0.895. Остаточный дефект — выбор целевого документа (7 кейсов).
План: few-shot в промпте правим ОДНИМ PR после первого end-to-end замера шага 3+4 (не плодить тюнинг).

## Открытые вопросы к лидеру
1. Node `self_check`: добавляем дешёвые линты markdown (заголовки, битые якоря) или минимально только JSON-схема + guard?
2. Outbox-формат для шага 4: один JSON на PR (payload со всеми файлами) устраивает?
