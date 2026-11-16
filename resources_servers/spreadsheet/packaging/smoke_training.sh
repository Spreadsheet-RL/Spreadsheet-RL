#!/usr/bin/env bash
set -eo pipefail
if [ "$#" -lt 2 ]; then
  echo "Usage: bash smoke_training.sh TRAINER_ROOT NLTK_DATA [smoke options]" >&2
  exit 2
fi
spreadsheet_trainer="$1"
spreadsheet_nltk="$2"
shift 2
spreadsheet_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
source "$spreadsheet_trainer/custom_utils/utils_training/use_custom_opt.sh" "$spreadsheet_trainer" "$spreadsheet_nltk"
set -euo pipefail
exec python -B -u "$spreadsheet_dir/smoke_training.py" --trainer-root "$spreadsheet_trainer" "$@"
