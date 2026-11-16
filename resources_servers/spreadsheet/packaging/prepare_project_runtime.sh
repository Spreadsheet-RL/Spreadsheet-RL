#!/usr/bin/env bash
# Reuse existing project imports; write only the explicitly selected runtime root.
set -eo pipefail
if [ "$#" -lt 2 ]; then
  echo "Usage: bash prepare_project_runtime.sh TRAINER_ROOT NLTK_DATA [Python options]" >&2
  exit 2
fi
spreadsheet_trainer_root="$1"
spreadsheet_nltk_data="$2"
shift 2
spreadsheet_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
spreadsheet_activate="$spreadsheet_trainer_root/custom_utils/utils_training/use_custom_opt.sh"
export PYTHONDONTWRITEBYTECODE=1
if ! source "$spreadsheet_activate" "$spreadsheet_trainer_root" "$spreadsheet_nltk_data" >/dev/null 2>&1; then
  echo "EXISTING_RUNTIME_ACTIVATION_FAILED" >&2
  exit 125
fi
set -euo pipefail
exec python -B -u "$spreadsheet_script_dir/prepare_project_runtime.py" --trainer-root "$spreadsheet_trainer_root" "$@"
