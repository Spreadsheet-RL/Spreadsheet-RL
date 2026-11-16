#!/usr/bin/env bash
# Reuse the same environment activation as the user's existing training launcher.
set -eo pipefail
umask 077
if [ "$#" -lt 3 ]; then
  echo "Usage: bash prepare_training_node.sh TRAINER_ROOT NLTK_DATA TRAINER_CONFIG [Python options]" >&2
  exit 2
fi
spreadsheet_trainer_root="$1"
spreadsheet_nltk_data="$2"
spreadsheet_trainer_config="$3"
shift 3
spreadsheet_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
spreadsheet_activate="$spreadsheet_trainer_root/custom_utils/utils_training/use_custom_opt.sh"
test -f "$spreadsheet_activate"
cd -- "$(dirname -- "$spreadsheet_trainer_config")"
# Activation may print arbitrary environment values; do not copy them into logs.
if ! source "$spreadsheet_activate" "$spreadsheet_trainer_root" "$spreadsheet_nltk_data" >/dev/null 2>&1; then
  echo "EXISTING_RUNTIME_ACTIVATION_FAILED: inspect the existing use_custom_opt.sh" >&2
  exit 125
fi
set -euo pipefail
exec python -u "$spreadsheet_script_dir/prepare_training_node.py" \
  --trainer-root "$spreadsheet_trainer_root" --trainer-config "$spreadsheet_trainer_config" \
  --activation-script "$spreadsheet_activate" "$@"
