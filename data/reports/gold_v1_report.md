# Отчёт: Gold Dataset v1.1

Дата: 2026-10-05. Схема: `data/gold/schema.json` (gold-v1).
Ветка v1.1: +1 negative-кейс (style_format_only доведён до 4), +2 bootstrap-stage
(Stage 2 README и Stage 5 changelog), id сдвинуты без коллизий. Вывод ниже —
результат `python scripts/validate_gold.py`.

```text
OK   gold-001.json [api_change -> write_docs]
OK   gold-002.json [api_change -> write_docs]
OK   gold-003.json [api_change -> write_docs]
OK   gold-004.json [api_change -> write_docs]
OK   gold-005.json [api_change -> write_docs]
OK   gold-006.json [api_change -> write_docs]
OK   gold-007.json [feature_doc -> write_docs]
OK   gold-008.json [feature_doc -> write_docs]
OK   gold-009.json [feature_doc -> write_docs]
OK   gold-010.json [feature_doc -> write_docs]
OK   gold-011.json [adr_impact -> write_docs]
OK   gold-012.json [adr_impact -> write_docs]
OK   gold-013.json [adr_impact -> write_docs]
OK   gold-014.json [adr_impact -> write_docs]
OK   gold-015.json [readme_update -> write_docs]
OK   gold-016.json [readme_update -> write_docs]
OK   gold-017.json [readme_update -> write_docs]
OK   gold-018.json [readme_update -> write_docs]
OK   gold-019.json [readme_update -> write_docs]
OK   gold-020.json [readme_update -> write_docs]
OK   gold-021.json [typo_fix -> stay_silent]
OK   gold-022.json [typo_fix -> stay_silent]
OK   gold-023.json [typo_fix -> stay_silent]
OK   gold-024.json [typo_fix -> stay_silent]
OK   gold-025.json [internal_refactor -> stay_silent]
OK   gold-026.json [internal_refactor -> stay_silent]
OK   gold-027.json [internal_refactor -> stay_silent]
OK   gold-028.json [internal_refactor -> stay_silent]
OK   gold-029.json [internal_refactor -> stay_silent]
OK   gold-030.json [ci_test_only -> stay_silent]
OK   gold-031.json [ci_test_only -> stay_silent]
OK   gold-032.json [ci_test_only -> stay_silent]
OK   gold-033.json [ci_test_only -> stay_silent]
OK   gold-034.json [style_format_only -> stay_silent]
OK   gold-035.json [style_format_only -> stay_silent]
OK   gold-036.json [style_format_only -> stay_silent]
OK   gold-037.json [style_format_only -> stay_silent]
OK   gold-038.json [docs_only -> stay_silent]
OK   gold-039.json [docs_only -> stay_silent]
OK   gold-040.json [bootstrap -> escalate_bootstrap]
OK   gold-041.json [bootstrap -> escalate_bootstrap]
OK   gold-042.json [bootstrap -> escalate_bootstrap]
OK   gold-043.json [bootstrap -> escalate_bootstrap]
OK   gold-044.json [bootstrap -> escalate_bootstrap]

=== Сводка ===
Кейсов: 44 (минимум для MVP: 30)
Negative (агент молчит): 19 = 43% (минимум 30%)
Категории:
   6  api_change
   6  readme_update
   5  internal_refactor
   5  bootstrap
   4  feature_doc
   4  adr_impact
   4  typo_fix
   4  ci_test_only
   4  style_format_only
   2  docs_only
Языки: {'ru': 21, 'en': 23}
Источник: {'synthetic': 39, 'bootstrap_scan': 5}
Триггеры: {'api_new': 5, 'api_signature_changed': 2, 'breaking_change': 4, 'api_removed': 1, 'behavior_changed': 4, 'dependency_added': 7, 'config_or_env_changed': 4, 'new_user_feature': 7, 'adr_new': 3, 'adr_changed': 2, 'none': 19}

=== Вердикт ===
  [OK] Gold-сет валиден и сбалансирован

```
