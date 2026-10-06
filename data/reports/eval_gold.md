# Eval — gold-регрессия (шаг 6)

**Итог: PASS**

| Проверка | Значение | Порог | Статус |
|---|---|---|---|
| `rules.behavior_accuracy` | 0.8889 | порог ≥ 0.85 | ✅ |
| `rules.fp_rate_negative` | 0.0 | порог ≤ 0.05 (шум в PR на negative-кейсах) | ✅ |
| `agent.drafts_produced_rate` | 0.7778 | порог ≥ 0.6 | ✅ |
| `agent.target_match_rate` | 1.0 | порог ≥ 0.8 | ✅ |

## Правила (шаг 1, без LLM)

- кейсов: 54 ({'write_docs': 27, 'stay_silent': 21, 'escalate_bootstrap': 6})
- behavior accuracy: 0.8889
- FP на negative: 0.0
- trigger coverage: 0.3962 (не гейтится)
- target coverage: 0.4324 (не гейтится)
- ошиблись: gold-009, gold-016, gold-017, gold-018, gold-019, gold-020

## Агент (шаг 2)

- backend: `fake`, write-кейсов: 27
- drafts_produced_rate: 0.7778
- target_match_rate: 1.0
- must_include_hit_rate: 0.8095
- hallucination_free_rate: 1.0
- dropped_total: 0
- target mismatch: gold-009, gold-016, gold-017, gold-018, gold-019, gold-020
