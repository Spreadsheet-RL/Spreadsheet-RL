#!/bin/bash

if [ $# -eq 0 ]; then
    echo "Usage: $0 <branch_name>"
    exit 1
fi

BRANCH_NAME="$1"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

declare -a EXCLUDE_FILES=(
    "sandbox/tests/datasets/samples/*.jsonl"
    "runtime/python/install-python-runtime.sh"
    "poetry.lock"
    "pyproject.toml"
    "Makefile"
    ".gitattributes"
    "*.png"
    "*.ico"
    "*.jar"
    "runtime/bin/bash"
    "sandbox/database.py"
    "sandbox/datasets/repobench_p.py"
    "sandbox/tests/datasets/test_repobench_p.py"
    "sandbox/tests/datasets/test_mbpp.py"
    "scripts/Dockerfile.base"
    "scripts/Dockerfile.server"
    "sandbox/datasets/humaneval.py"
    "scripts/asset/sd"
    "sandbox/configs/*.yaml"
    "sandbox/tests/datasets/samples/seed.code.*.jsonl"
    "assets/tokenizer/bbpe155k-v6.4.3-ml.pret/*"
    "sandbox/datasets/autoeval_v4.py"
    "sandbox/datasets/autoeval_v5.py"
    "sandbox/datasets/autoeval_v6.py"
    "sandbox/datasets/codenet_atcoder.py"
    "sandbox/datasets/codenet_fix.py"
    "sandbox/datasets/cuda.py"
    "sandbox/datasets/custom.py"
    "sandbox/datasets/dp_eval.py"
    "sandbox/datasets/python_auto.py"
    ".codebase/*"
    "sandbox/tests/datasets/test_autoeval*v*"
    "sandbox/tests/datasets/test_chinaoj.py"
    "sandbox/tests/datasets/test_codenet_fix_v1.py"
    "sandbox/tests/datasets/test_cuda_dataset.py"
    "sandbox/tests/datasets/test_custom.py"
    "sandbox/tests/datasets/test_dp_eval.py"
    "sandbox/tests/datasets/test_libreoj.py"
    "sandbox/tests/datasets/test_mining_11697_v1.py"
    "sandbox/tests/datasets/test_python_auto.py"
    "sandbox/tests/datasets/test_rl_oj_*.py"
    "sandbox/tests/runners/test_lean.py"
    "scripts/git/*"
    "scripts/Dockerfile.faas"
    "scripts/Dockerfile.merlin"
    "scripts/export_datalake.py"
    "scripts/run_*.sh"
    "scripts/stress_test.py"
    "scripts/faas/*"
    "scripts/Dockerfile.cuda*"
    "sandbox/tests/datasets/test_can_it_edit.py"
    "sandbox/tests/datasets/test_minif2f_lean4.py"
    "scripts/upload_pypi.sh"
    "LICENSE"
    "assets/tokenizer/*"
    "sandbox/configs/__init__.py"
)
EXCLUDE_ARGS=""
for file in "${EXCLUDE_FILES[@]}"; do
    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$file"
done

echo "Merging branch: $BRANCH_NAME"
git diff "$CURRENT_BRANCH".."$BRANCH_NAME" > changes.patch
git apply $EXCLUDE_ARGS changes.patch
rm -f changes.patch

echo "Merge and cleanup completed"