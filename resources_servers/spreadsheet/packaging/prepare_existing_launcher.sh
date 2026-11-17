#!/usr/bin/env bash
# Prepare YAML and a separate launcher using the original runtime activation.
set -eo pipefail
umask 077
if [ "$#" -lt 3 ]; then
  echo 'Usage: bash prepare_existing_launcher.sh TRAINER_ROOT NLTK_DATA TRAINER_CONFIG [Python options]' >&2
  exit 2
fi
spreadsheet_trainer_root="$1"
spreadsheet_nltk_data="$2"
spreadsheet_trainer_config="$3"
shift 3
spreadsheet_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$(dirname -- "$spreadsheet_trainer_config")"
export PYTHONDONTWRITEBYTECODE=1
# This is already set by the supplied training launcher for its custom checkout.
export NRL_IGNORE_VERSION_MISMATCH=1
if ! source "$spreadsheet_trainer_root/custom_utils/utils_training/use_custom_opt.sh" \
  "$spreadsheet_trainer_root" "$spreadsheet_nltk_data" >/dev/null 2>&1; then
  echo 'EXISTING_RUNTIME_ACTIVATION_FAILED' >&2
  exit 125
fi
set -euo pipefail
exec python -B -u "$spreadsheet_script_dir/prepare_existing_launcher.py" \
  --config "$spreadsheet_trainer_config" "$@"
