# File-based spreadsheet integration

Development branch: `tungks/feat/tool/spreadsheet-rl`.

Start with [SETUP.md](../SETUP.md) for the service/trainer split and
the runtime reproducibility contract. `runtime.lock.json` pins the published
dependency artifact. `modal_reproduce.py` checks two independent offline Ubuntu
installs, sandbox reconstruction, package/content fingerprints and real canaries;
the worker logic is in `verify_runtime.py`. No runtime is installed on the Mac.
The qualified 2026-11-17 repair is the default lock; its two independent builds
produced identical archives, and two further clean offline installs passed.
See [reproducibility-20261117.json](reproducibility-20261117.json) for evidence.
`runtime.legacy.lock.json` retains the original four-file glibc drift;
`runtime.candidate.lock.json` preserves the exact pre-promotion test input.

Terminal commands should download an immutable release, verify its checksum, and
invoke these files with node-specific paths. Keep conversion, contract checks,
source installation and YAML edits in the versioned scripts. Operator-specific
paths and private keys stay outside public release artifacts.

## Audit rejected tasks on Modal CPU

`modal_audit_dataset.py` installs the pinned dependency artifact once into a
cached image, downloads/checksums the pinned public data on Modal, and audits
8,647 tasks with up to **eight independent CPU workers**. Each 50-task batch
has a source/data/baseline identity and a hashed completion checkpoint. Resume
by passing the same `--run-id`, full clean `--source-commit`, and batch size.

```bash
modal run --profile PROFILE resources_servers/spreadsheet/packaging/modal_audit_dataset.py \
  --source-commit "$(git rev-parse HEAD)" --output-dir /path/to/new/audit-results
```

The baseline reader is loaded from the original source inside the locked bundle.
The new scorer streams values, supports up to 4,096 metadata ranges and a total
of two million answer cells; per-range and public tool limits remain bounded.
Original exact/normalized cell equality is preserved, including the two-decimal
rounding boundary. Numeric precision candidates are diagnostic labels only.

Each task receives two independent oracle recalculations. Strict recoveries
also undergo a temporary native-tool add/write/remove-sheet and recalc roundtrip.
`recovered_scoring_only` tasks are withheld from recovered training rows.
`recovered.manifest.jsonl` and `recovered.{train,test}.jsonl` contain only strict,
tool-ready recoveries. This does not rewrite the active dataset or training YAML.
The original node's historical ID membership must be checked separately if its
per-task report is unavailable; matching a total alone does not establish it.

`summary.json` contains counts, provenance and output hashes. Detailed per-cell
reports and formulas stay in the private Modal Volume and local audit directory,
outside public source bundles and policy observations. The worker implementation
is `audit_rejections.py`; `--limit` is available for partial diagnostic runs.

`audit_selection.py --tasks TASKS.jsonl --rejected rejected.audit.jsonl
--summary summary.json --output NEW_COVERAGE.json` reports retention by split,
task source and answer-range count. It validates ID uniqueness and audit totals.
Zero regressions establishes preservation on the observed baseline pass cohort;
it does not establish absence of selection bias. Keep strict and any future
LibreOffice compatibility scores separate. Calibrate a new value policy on train
only and freeze it before held-out evaluation; never tune task-specific rules
against oracle answers. The current audit does not relax numeric equality.

`modal_inspect_dependencies.py` reads raw original/recalculated OOXML using the
already-built runtime image selected by `SPREADSHEET_AUDIT_BASE_IMAGE`. It records
date/random-function hints, external-link parts and invalid filter metadata,
without normalizing the oracle or silently removing workbook features. These
private diagnostics distinguish volatile dependencies from engine differences.

| Entrypoint | Required inputs | Result |
| --- | --- | --- |
| `convert_training_data.py` | `--input-dir` containing both qualified JSONL files; `--output-dir` | Routed `train.jsonl` / `test.jsonl`, conversion hashes and a separate run receipt |
| Same, with YAML update | Also `--trainer-root` and `--trainer-config`; optional `--contract` | Verify pinned dataset/processor AST first, back up YAML, replace only its single training data path |
| `install_gym_integration.py` | `--gym-root`, `--trainer-config`, `--private-config`, `--receipt`; optional `--source-root` | Install the two frontend modules, register spreadsheet config then private override, write file/config hashes |
| `start_service.py` | Existing runtime bundle, qualified catalog, node-local service root and sandbox root receipt; optional `--export-private-config` | Start/reuse the API; export its Gym override to shared storage with mode 0600 |
| `prepare_training_node.sh` | `TRAINER_ROOT NLTK_DATA TRAINER_CONFIG` followed by the Python options below | Use the existing activation/cache helper, install source/config, convert data and check existing frontend venv imports |
| `prepare_cached_frontends.py` | `--venv-root`, `--expected-hostname`, `--receipt` | Copy the existing `math_with_judge` and `simple_agent` caches for the two new frontends; no installs or venv aliases |
| `prepare_project_runtime.sh` | `TRAINER_ROOT NLTK_DATA`, then `--base-config`, `--runtime-root`, `--qualified-dir`, `--output-dir`, `--private-config`, `--expected-hostname` | Prepare project-local caches and an inherited YAML; verify existing integration/data without modifying their files |
| `check_live_service.py` | `--private-config`, `--dataset`, `--manifest`, `--data-root`, fresh `--output-dir`, `--expected-hostname` | Trusted HTTP canary covering all 12 tools, isolation, recalc, replay and reward; separate from model rollouts |
| `smoke_training.sh` | `TRAINER_ROOT NLTK_DATA`, then `--base-config`, `--run-dir`, short fresh `--ipc-root`, `--private-config`, `--expected-hostname` | Two optimizer steps with two prompts and two generations each; existing weights/actor venvs, separate logs, disabled checkpoints and external logging |
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

## Convert Spreadsheet prompts for Qwen3-Coder

To test Spreadsheet with a shared `qwen3_coder` parser, run the prompt converter
next to the existing qualified dataset:

```bash
python3 convert_coder_prompts.py \
  --input-dir /path/to/prepared/nemo_gym \
  --manifest /path/to/prepared/libreoffice/manifest.qualified.jsonl \
  --output-dir /path/to/prepared/nemo_gym_qwen3_coder_v2
```

The converter adds the Coder call syntax to the Spreadsheet system message and
advertises `fill_formula.end_row` as an optional integer with its bounds at the
property level. Callers can omit it. This lets legacy Coder parsers read the type
without inspecting nullable `anyOf` branches. The converter keeps all other row
fields and checks both splits against the original manifest before writing a
separate pair of JSONL files with checksum receipts.
This step requires only Python on CPU. A rollout smoke must confirm that the
checkpoint follows the example and that the server returns structured
`function_call` and `function_call_output` entries.

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

The live service canary creates two fresh sessions from one qualified task. It
exercises only a temporary QA sheet, removes it before verification, checks the
original seed/oracle hashes, and closes both sessions. Its tool calls are trusted
test actions and must never be counted as model-generated actions or mixed into
training data. A report preserves operation IDs and any unconfirmed cleanup.

The training smoke uses the first four unchanged converted rows, a generated
inherited YAML, and the model's actual tokenizer tool format/EOS. It prunes
unrelated inherited judge definitions, limits generation concurrency and agent
turns, and preserves the existing asynchronous Megatron/vLLM setup. It requires
idle allocated GPUs and all prebuilt actor environments; no dependency bootstrap
is attempted. Known service credentials are removed from captured stdout before
writing `train.log`. `run.json` preserves config/data/source hashes, GPU UUIDs,
driver ownership, duration and exit status. A successful process exit is not by
itself proof of tool calls or correct rewards: inspect the retained Gym response
JSONL and optimizer metrics before reporting an end-to-end pass.
Pass a fresh short `--ipc-root` such as `/workspace/storage-shared/nlp/tungks/a`:
both Ray and vLLM create Unix sockets subject to Linux's 107-byte path limit.
The helper validates their path lengths and keeps temporary files in that root;
the longer `--run-dir` still holds configuration, caches, logs and receipts.
`smoke_driver.py` supplies the existing NeMo-RL `init_ray(log_dir=...)` hook so
Ray's dashboard sockets also fit. It then runs the unchanged training entrypoint;
no trainer source or global Ray configuration is modified.
After the run, `analyze_training_smoke.py --run-dir RUN --output NEW_REPORT.json`
counts only model `response.output` tool calls and compares verifier rewards to
the retained training rewards. It reports tool/environment errors and missing
responses without printing workbook contents. Optimizer metrics need separate
inspection; this audit does not infer updates from filenames or an exit code.
Some trainer checkouts hardcode `full_result` to retain only prompt inputs.
For this case, `audit_agent.py` subclasses the existing agent and appends its
unmodified verifier result to the private run's `model_rollouts.jsonl`, omitting
large token/logprob arrays. The smoke config selects this entrypoint through
Gym's relative-entrypoint option and a single worker; the shared frontend source
is unchanged. The analyzer matches prompt-content/reward multisets to training
rows and excludes unused prefetched rollouts from its call counts.
The report records advantage bounds too. All-zero advantages demonstrate
tool/reward routing but do not demonstrate a useful policy learning update.

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
