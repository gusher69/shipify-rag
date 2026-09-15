# Thai human-language intelligence lab

PyThaiNLP 5.3.7 · RapidFuzz 3.14.6 · offline tier · 792.1s

| Metric | Value | Gate |
|---|---|---|
| Base ground truth | 55/55 | 100% |
| Typo cases | 486 | >= 300 |
| Typo semantic recovery | 99.18% (482/486) | >= 95% |
| Paraphrase cases | 324 in 40 groups | >= 300 |
| Paraphrase intent consistency | 100.0% (324/324) | >= 98% |
| Identifier mutation | 0 / 12 | 0 |
| Unknown product preservation | 12/12 | 100% |
| Over-correction (real phrases changed) | 0 / 37 | 0 |
| Journeys | 100 (560 turns, 333 noisy) | >= 100 |
| Context continuity | 100.0% turns, 100/100 journeys | — |
| Known slot re-ask | 0 | 0 |
| Stale context takeover | 0 | 0 |
| Auth violation | 0 | 0 |
| Private leak | 0 | 0 |
| False completion | 0 | 0 |
| Hallucinated business fact | 0 | 0 |

## Typo recovery by operator

| operator | passed | total |
|---|---|---|
| add_particle | 34 | 34 |
| combined | 48 | 49 |
| double_final | 37 | 37 |
| drop_mark | 50 | 53 |
| drop_thanthakhat | 4 | 4 |
| dup_mark | 55 | 55 |
| elongate | 41 | 41 |
| extra_spaces | 13 | 13 |
| hand | 49 | 49 |
| keyboard | 15 | 15 |
| nbsp | 13 | 13 |
| particle | 19 | 19 |
| remove_spaces | 12 | 12 |
| swap_tone | 37 | 37 |
| zero_width | 55 | 55 |

## Failures

### base: 0

### typo: 4
- ` อยากสังของจากจีน 15 ชิ้น` -> ["unexpected product 'สังของ'"]
- ` สนใจนำเข่ากลองพลาสติก 5 ลัง ส่งทางเรือ` -> ["product 'กลองพลาสติก' != 'กล่องพลาสติก'"]
- ` ตองการนำเข้าของเล่นจากจีน` -> ["product 'ตองการของเล่น' != 'ของเล่น'"]
- ` ขอเปลี่ยนท่อยู่จัดส่ง` -> ['family UNKNOWN != ADDRESS_CHANGE']

### paraphrase: 0

### identifiers: 0

### unknown_products: 0

### over_correction: 0

### journeys: 0
