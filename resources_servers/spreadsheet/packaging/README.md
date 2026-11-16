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
| `start_service.py` | Existing runtime bundle, qualified catalog, node-local service root and sandbox root receipt; optional `--export-private-config` | Start/reuse the API; export its Gym override to shared storage with mode 0600 |
| `prepare_training_node.sh` | `TRAINER_ROOT NLTK_DATA TRAINER_CONFIG` followed by the Python options below | Use the existing activation/cache helper, install source/config, convert data and check existing frontend venv imports |
| `prepare_cached_frontends.py` | `--venv-root`, `--expected-hostname`, `--receipt` | Copy the existing `math_with_judge` and `simple_agent` caches for the two new frontends; no installs or venv aliases |
| `prepare_project_runtime.sh` | `TRAINER_ROOT NLTK_DATA`, then `--base-config`, `--runtime-root`, `--qualified-dir`, `--output-dir`, `--private-config`, `--expected-hostname` | Prepare project-local caches and an inherited YAML; verify existing integration/data without modifying their files |
| `build_source_bundle.py` | Clean source checkout; `--output-dir` outside it | Small source archive and checksum, pinned to the exact Git commit |

Conversion, source installation and source packaging use only Python's standard
library. Conversion and source installation do not require `git`, PyYAML,
LibreOffice, CUDA, or package downloads on the node. The source-bundle builder
uses Git on the development machine.

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

## Reuse the existing training launcher environment

The node wrapper sources the existing
`custom_utils/utils_training/use_custom_opt.sh TRAINER_ROOT NLTK_DATA`, from the
training YAML's directory, just like the current launcher. It invokes
`prepare_training_node.py` with that active Python. The helper requires
`--qualified-dir`, `--output-dir`, `--private-config`, `--run-dir`, and
`--expected-hostname`; the wrapper supplies `--trainer-root` and
`--trainer-config`. The active interpreter must already import the selected
`nemo_rl` checkout and `nemo_gym`.

Before mutation, it checks the dataset contract, existing cache helper,
`skip_venv_if_present: true`, and direct connectivity to the qualified service.
It then installs the frontend files, appends Gym config paths, converts both
splits, patches the single training data path, and runs the existing
`copy_gym_venvs.sh TRAINER_CONFIG`. This delegates to the project's cache
mechanism without creating custom aliases or another venv layout. The helper
receives `UV_OFFLINE=1` and `PIP_NO_INDEX=1`; no network package bootstrap is
needed. It finally imports the frontend modules from their existing sub-venvs
and checks Ray/OpenAI versions against the existing model server's venv.

The inspected project cache helper only recognizes relative resource config
paths and does not prepare custom agents. Before invoking it, setup therefore
copies the existing `math_with_judge` cache for `spreadsheet`, and the existing
`simple_agent` cache for `spreadsheet_agent`, using `prepare_cached_frontends.py`.
Complete existing destinations are reused; linked or incomplete destinations
are preserved and rejected. This creates real cache copies without installing
dependencies or modifying the project's helper. On a Bridge binding whose write
root excludes the cache directory, use the project-local runtime below instead
of writing outside the binding policy. The standalone cache copier also accepts
`--source-root`, `--include-model`, and `--relocate-activation` for copying an
existing cache into a new location.

## Project-local runtime under an existing Bridge scope

`prepare_project_runtime.sh` sources the existing import activation and invokes
`prepare_project_runtime.py`. The active trainer must already have the matching
spreadsheet frontend sources, config paths, and verified converted data. It
checks these prerequisites without installing source, patching the base YAML,
recalculating workbooks, or recreating data.

All persistent writes are below the fresh `--runtime-root`. It copies existing
`math_with_judge`, `simple_agent`, and `vllm_model` caches from
`--source-cache-root` (default `/opt/gym_venvs`) into the project's resource,
agent, and model cache directories. Only copied Bash activation scripts are
retargeted. Import probes require the copied interpreter prefixes and matching
Ray/OpenAI versions. Python bytecode writes are disabled during preparation and
probes.

The new `spreadsheet.yaml` inherits `--base-config` and overrides only
`env.nemo_gym.uv_venv_dir`. Setup verifies the complete effective configuration
equals the base with that single change. Use this generated YAML when launching
training. `setup.json` pins the base/config/source/data hashes and live service
health. `TRAINING_NODE_PREPARED` confirms preparation; no training is launched.

Each invocation saves a private phase receipt and cache-helper log under a
unique setup directory. `TRAINING_NODE_PREPARED` means these setup checks passed;
the script does not launch training or prove a completed policy rollout.

Both nodes may read the same dataset/source on shared storage. Use
`start_service.py --export-private-config SHARED_PATH` once on the service node
to make its existing Gym override accessible to the trainer with mode 0600.
It refuses to replace different credentials. Keep this file out of public
archives. Python sandbox executables and the API's SQLite/lock state remain on
node-local storage; those constraints do not require private copies of the
dataset or source on each node.

## Source release contents

The builder packages only tracked spreadsheet/agent files plus the repository
license, with normalized archive metadata and a `RELEASE.json` file containing
the source commit and per-file SHA-256 values. Ignored caches, private files,
datasets, runtime binaries and untracked changes are not included. The builder
refuses a dirty worktree. Rebuilding the same clean revision with the same Python
compression implementation produces the same archive bytes.
