# Generalization Miss Analysis — Phase 6 closure gate D

Every residual miss from the 1517-turn lab, grouped by root cause. Nothing
is hidden inside an aggregate percentage: the lab now dumps **all**
failures (`all_failures` in `reports/phase6_generalization_lab.json`), not
a sample.

## Starting point (before this gate)

| Metric | Value | Misses |
|---|---|---|
| product accuracy | 98.06% | 25 |
| quantity accuracy | 99.56% | 4 |

## Classification of all 29 misses

### CLEAR_SYSTEM_ERROR — 29 of 29 (all now fixed)

**Class 1 — Thai repetition mark retained (25 misses).**

| | |
|---|---|
| input | `อยากนำเข้าชั้นวางของๆ` (and 24 more nouns, same shape) |
| expected | `ชั้นวางของ` |
| actual | `ชั้นวางของๆ` |
| root cause | `ๆ` is the Thai repetition mark and is never part of a noun's citation form, but neither product-noun extractor normalised it away. |
| fix | Both extractors now strip a trailing `ๆ`. One rule, both sites. |

**Class 2 — bottle quantity invisible to the slot-answer path (4 misses).**

| | |
|---|---|
| input | `20 ขวด`, `ประมาณ 300 ขวด` (as an answer to "how many?") |
| expected | quantity 20 / 300 |
| actual | `None` |
| root cause | The count-unit alternation existed in **five** separate copies (four in `conversation_semantics.py`, one in P1). Only `_USER_QTY_RE` carried `ขวด`/`พาเลท`, so the opener recognised a bottle quantity and every bare-slot-answer matcher did not. A drift generator, not a one-unit oversight. |
| fix | One `_COUNT_UNIT_ALT` constant substituted into all five sites; P1 derives its tuple from it. A new unit can only be added in one place. |

### AMBIGUOUS_LANGUAGE — 0
### LAB_MEASUREMENT_ARTIFACT — 0 (two earlier artifacts were corrected in the lab itself before this gate: bare slot answers are now probed through the component that actually resolves them)
### UNSUPPORTED_DOMAIN — 0
### SAFE_FALLBACK — 0

## Result after the fixes

| Metric | Value | Misses |
|---|---|---|
| product accuracy | **100.0%** | 0 |
| quantity accuracy | **100.0%** | 0 |
| unit preservation | **100.0%** | 0 |
| method accuracy | **100.0%** | 0 |
| correction accuracy | **100.0%** | 0 |
| entity / journey / safety failures | **0 / 0 / 0** | |

**CLEAR_SYSTEM_ERROR = 0.** Both classes were fixed at the shared
mechanism rather than explained away or absorbed into an average.
