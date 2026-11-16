# File-based spreadsheet integration

Development branch: `tungks/feat/tool/spreadsheet-rl`.

Terminal commands should download an immutable release, verify its checksum, and
invoke these files with node-specific paths. Keep conversion, contract checks,
source installation and YAML edits in the versioned scripts. Operator-specific
paths and private keys stay outside public release artifacts.

| Entrypoint | Required inputs | Result |
| --- | --- | --- |
| `convert_training_data.py` | `--input-dir` containing both qualified JSONL files; `--output-dir` | Routed `train.jsonl` / `test.jsonl`, conversion hashes and a separate run receipt |
| Same, with YAML update | Also `--trainer-root` and `--trainer-config`; optional `--contract` | Verify pinned dataset/processor AST first, back up YAML, replace only its single training data path |
| `install_gym_integration.py` | `--gym-root`, `--trainer-config`, `--private-config`, `--receipt`; optional `--source-root` | Install the two frontend modules, register spreadsheet config then private override, write file/config hashes |
| `build_source_bundle.py` | Clean source checkout; `--output-dir` outside it | Small source archive and checksum, pinned to the exact Git commit |

All three entrypoints use only Python's standard library. Conversion and source
installation do not require `git`, PyYAML, LibreOffice, CUDA, or package downloads
on the node. The source-bundle builder uses Git on the development machine.

## Dataset contract and receipts

The default contract is `../contracts/nemo_gym_raw_jsonl_v1.json`. It records the
Python minor version and normalized AST fingerprints of the raw-JSONL
`NemoGymDataset` wrapper and `nemo_gym_data_processor`. The verifier ignores
docstrings/comments but records each complete source file's SHA-256. A mismatch
stops before conversion or YAML changes. Review a changed implementation's data
flow before publishing a new contract; do not automatically accept its hash.

`conversion.json` pins source/output JSONL hashes and row counts. Every invocation
also writes `conversion-run-<id>.json`, or the new path supplied by `--receipt`,
with the tool/contract/source hashes, paths, Python version, and YAML before/after
hashes and private backup path. The actual API keys are never recorded. Repeated
conversion reuses existing files only when the hashes match.

Only `data.train` is changed by the converter. Existing model settings, algorithm
parameters, validation configuration and launcher remain byte-for-byte intact.

## Gym source installation

Select the active Gym source directory explicitly. The installer checks for its
base resource server and simple-agent source before copying this allowlist:

- `resources_servers/spreadsheet/{app.py,schemas.py,requirements.txt}`
- `resources_servers/spreadsheet/configs/spreadsheet.yaml`
- `responses_api_agents/spreadsheet_agent/{app.py,requirements.txt}`

It refuses conflicting files and incomplete existing module directories. A
complete matching installation is safe to repeat. Source copies are staged per
module and the receipt pins all six files. No backend, workbook or oracle is
copied into the Gym checkout.

The installer appends only the spreadsheet config and the operator's private
override to `env.nemo_gym.config_paths`, preserving other settings and saving a
private YAML backup. The private file must already exist with owner-only
permissions at a path the training container can read. Keep its values out of
Git/public archives. The installer records its path only.

Source installation is distinct from runtime readiness. Reuse the appropriate
existing Gym sub-venvs according to the active trainer's cache helper; this
installer does not provision them. Its receipt explicitly records
`runtime_import_check: not_run` and `training: not_started`. Verify imports and
one real tool/reward rollout in the training runtime before claiming readiness.

## Source release contents

The builder packages only tracked spreadsheet/agent files plus the repository
license, with normalized archive metadata and a `RELEASE.json` file containing
the source commit and per-file SHA-256 values. Ignored caches, private files,
datasets, runtime binaries and untracked changes are not included. The builder
refuses a dirty worktree. Rebuilding the same clean revision with the same Python
compression implementation produces the same archive bytes.
