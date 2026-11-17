# Dataset compatibility audit — 2026-11-17

Eight Modal CPU workers checked all 8,647 tasks using the pinned runtime and
dataset. Audit source: `f86fbf2c98db97d9cdf19a3c81d148ea02b89cc6`.
Each oracle was recalculated independently twice, with original bytes preserved.
The frozen baseline reader came from the locked runtime bundle. The new reader
streams values and separates answer-metadata limits from the public tool limit;
the original exact/type/normalization/rounding rules remain unchanged.

| Result | Tasks |
| --- | ---: |
| Frozen baseline passed in this run | 7,685 |
| Frozen baseline rejected | 962 |
| Recovered with strict values and a native-tool roundtrip | 4 |
| Scoring recovered, native tools still fail | 21 |
| Numeric precision candidates, still excluded | 47 |
| Unstable or unconfirmed independent recalculations | 30 |
| Remaining mismatches, missing values/sheets and errors | 860 |
| Regressions among baseline-passing tasks | **0** |

The historical node log had 7,684 passing tasks. Its per-task report was not
available to this run; a one-task count difference does not identify which IDs
changed. The new audit is a separately reproduced baseline, not a verified copy
of the historical accepted/rejected membership. Two recalculations also cannot
guarantee stable results for every date or random state.

## Recovered data and deployment

The four strict recoveries are training tasks with more than 20 answer regions.
They pass both recalculations and an add/write/remove-sheet/recalculate roundtrip.
`recovered.manifest.jsonl` and `recovered.train.jsonl` are exported privately;
the recovered test split is empty. They require the updated service source.
The active company service, original dataset and training YAML were not changed.
The 21 scoring-only recoveries remain excluded from training exports.

## Remaining causes and next repairs

Of the 104 unresolved errors, the first-attempt causes are 69 unsupported A1
metadata forms, 22 worksheet-resolution failures, 10 per-range cell limits and
three LibreOffice timeouts. Parser repairs should follow the original upstream
`reward/async_reward_api/eval.py` contract, with source position and canonical
position recorded separately. These errors must not all be labeled engine bugs.

Raw OOXML inspection confirmed `TODAY()` in a previously reported changing
result and `RANDBETWEEN()` in an unstable task. A time/random-dependent task needs
an explicit reproducible evaluation context or a separate track; changing the
cached oracle to today's LibreOffice result is not a strict recovery.

Two original XML examples export an invalid `customFilter operator="**none**"`.
The relevant metadata is an AutoFilter, not conditional formatting. Other native
reader failures include invalid numeric, range and date metadata. Ignoring
formatting for value scoring does not establish that editing/resaving the whole
workbook works. The roundtrip gate prevents that unsupported promotion.

Precision candidates are diagnostic labels only: all observed mismatches in the
answer region must be finite numeric differences within four ULPs and an absolute
cap of 1e-7. This rule does **not** change reward or accept those tasks. A future
compatibility value policy needs its own version, train-only calibration and
held-out evaluation, reported separately from strict scoring.

## Selection bias

| Answer-region count | Original tasks | Baseline kept | Strict recovered |
| --- | ---: | ---: | ---: |
| 1 | 8,054 | 7,229 (89.76%) | 0 |
| 2–5 | 562 | 432 (76.87%) | 0 |
| 6–20 | 26 | 24 (92.31%) | 0 |
| 21+ | 5 | 0 | 4 |

Train retention was 5,283/5,925 (89.16%); test retention was 2,402/2,722 (88.24%).
Sources and task complexity differ between splits, so these are descriptive
coverage statistics, not a causal estimate of engine bias. Zero regressions does
not prove an unbiased subset or Excel equivalence for future policy formulas.
Keep the full task inventory and exclusion reasons; preserve strict and any
future LibreOffice compatibility tracks separately.

## Evidence

[dataset-audit-20261117.json](packaging/dataset-audit-20261117.json) contains safe
counts, coverage, immutable runtime/data references, source IDs and report hashes.
Private oracle diagnostics and recovered rows are on Modal Volume
`spreadsheet-gym-runtime-builds`, under
`audit-runs/strict-recovery-f86fbf2c98db/`. They must not enter policy observations
or public source bundles.

Focused Linux regressions: **15 passed, 1 skipped**; full local integration suite:
**104 passed, 7 skipped** using the existing cached environment. The Linux audit
itself ran real LibreOffice recalculations. Changed-file Ruff checks passed.
