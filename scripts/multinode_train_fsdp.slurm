#!/bin/bash -l
#SBATCH --job-name=train-verl-nodes
#SBATCH --partition=<partition>
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=288
#SBATCH --gpus-per-node=4
#SBATCH --mem=748G
#SBATCH --time=02:00:00
#SBATCH --account=<account>
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_repo_root_from() {
    local dir="$1"
    while [[ -n "${dir}" && "${dir}" != "/" ]]; do
        if [[ -d "${dir}/verl/verl" && -d "${dir}/configs" && -f "${dir}/scripts/multinode_train_fsdp.slurm" ]]; then
            echo "${dir}"
            return 0
        fi
        dir="$(dirname "${dir}")"
    done
    return 1
}

resolve_repo_root() {
    local script_dir="$1"
    local job_info=""
    local job_work_dir=""
    local job_command=""
    local candidate=""
    local root=""

    if [[ -n "${SLURM_JOB_ID:-}" ]]; then
        job_info="$(scontrol show job "${SLURM_JOB_ID}" 2>/dev/null || true)"
        job_work_dir="$(sed -n 's/.* WorkDir=\([^ ]*\).*/\1/p' <<<"${job_info}" | head -n1)"
        job_command="$(sed -n 's/.* Command=\([^ ]*\).*/\1/p' <<<"${job_info}" | head -n1)"

        for candidate in "${job_work_dir}" "${SLURM_SUBMIT_DIR:-}"; do
            if [[ -n "${candidate}" ]]; then
                root="$(find_repo_root_from "${candidate}" || true)"
                if [[ -n "${root}" ]]; then
                    echo "${root}"
                    return 0
                fi
            fi
        done

        if [[ -n "${job_command}" ]]; then
            candidate="$(dirname "${job_command}")"
            if [[ -d "${candidate}" ]]; then
                root="$(find_repo_root_from "$(cd "${candidate}" && pwd)" || true)"
                if [[ -n "${root}" ]]; then
                    echo "${root}"
                    return 0
                fi
            fi
        fi
    fi

    for candidate in "${SLURM_SUBMIT_DIR:-}" "${PWD:-}"; do
        if [[ -n "${candidate}" ]]; then
            root="$(find_repo_root_from "${candidate}" || true)"
            if [[ -n "${root}" ]]; then
                echo "${root}"
                return 0
            fi
        fi
    done

    find_repo_root_from "${script_dir}"
}

REPO_ROOT="${REPO_ROOT:-$(resolve_repo_root "${SCRIPT_DIR}")}"
if [[ -z "${REPO_ROOT}" ]]; then
    echo "Error: failed to resolve REPO_ROOT. Set REPO_ROOT or submit the job from inside the repo." >&2
    exit 1
fi

canonicalize_existing_path() {
    local path="$1"
    local dir=""
    dir="$(cd "$(dirname "${path}")" && pwd -P)"
    printf '%s/%s\n' "${dir}" "$(basename "${path}")"
}

resolve_existing_path() {
    local path="$1"
    shift || true
    local base=""

    if [[ -z "${path}" ]]; then
        return 1
    fi

    if [[ -e "${path}" ]]; then
        canonicalize_existing_path "${path}"
        return 0
    fi

    for base in "$@"; do
        if [[ -n "${base}" && -e "${base}/${path}" ]]; then
            canonicalize_existing_path "${base}/${path}"
            return 0
        fi
    done

    return 1
}

expand_slurm_job_id_placeholder() {
    local value="$1"
    local job_id="${SLURM_JOB_ID:-$$}"
    value="${value//\$\{SLURM_JOB_ID\}/${job_id}}"
    value="${value//\$SLURM_JOB_ID/${job_id}}"
    printf '%s\n' "${value}"
}

MODULE_PYTHON="${MODULE_PYTHON:-python/miniforge3_pytorch/2.10.0}"
MODULE_NCCL_OFI="${MODULE_NCCL_OFI:-nccl-ofi-plugin}"
MODULE_CUDNN="${MODULE_CUDNN:-cudnn/9.3.0.75}"
if [[ -z "${VENV_DIR:-}" ]]; then
    if [[ -f "${REPO_ROOT}/.venv/bin/activate" ]]; then
        VENV_DIR="${REPO_ROOT}/.venv"
    fi
fi
VENV_DIR="${VENV_DIR:-}"
PROJECT_DIR="${PROJECT_DIR:-${REPO_ROOT}}"
VERL_DIR="${VERL_DIR:-${PROJECT_DIR}/verl}"
RAY_DATA_HOME="${RAY_DATA_HOME:-${PROJECT_DIR}/verl_data}"
GDRCOPY_HOME="${GDRCOPY_HOME:-${HOME}/.local/gdrcopy/2.5.1}"
NVSHMEM_DIR="${NVSHMEM_DIR:-/sw/user/python/miniforge3-pytorch-2.10.0/lib/python3.12/site-packages/nvidia/nvshmem}"
CUDNN_HOME="${CUDNN_HOME:-/sw/user/cudnn/cudnn-linux-sbsa-9.3.0.75_cuda12-archive}"
CUDNN_PATH="${CUDNN_PATH:-${CUDNN_HOME}}"
MODEL_PATH="${MODEL_PATH:-}"

SANDBOX_FUSION_SIF="${SANDBOX_FUSION_SIF:-}"
if [[ -z "${SANDBOX_FUSION_SIF}" ]]; then
    REPO_PARENT_DIR="$(cd "${PROJECT_DIR}/.." && pwd)"
    for cand in \
        "${PROJECT_DIR}/sandboxfusion_workspace.sif" \
        "${PROJECT_DIR}/sandboxfusion.sif" \
        "${PROJECT_DIR}/sandbox/SandboxFusion.sif" \
        "${PROJECT_DIR}/sandbox/SandboxFusion.aarch64.sif" \
        "${REPO_PARENT_DIR}/sandboxfusion_workspace.sif" \
        "${REPO_PARENT_DIR}/sandboxfusion.sif" \
        "${REPO_PARENT_DIR}/sandbox/SandboxFusion.sif" \
        "${REPO_PARENT_DIR}/sandbox/SandboxFusion.aarch64.sif"; do
        if [[ -f "${cand}" ]]; then
            SANDBOX_FUSION_SIF="${cand}"
            break
        fi
    done
fi
SANDBOX_FUSION_HOST="${SANDBOX_FUSION_HOST:-127.0.0.1}"
SANDBOX_FUSION_PORT="${SANDBOX_FUSION_PORT:-8080}"
SANDBOX_FUSION_URL="${SANDBOX_FUSION_URL:-http://${SANDBOX_FUSION_HOST}:${SANDBOX_FUSION_PORT}/run_code}"

HF_REPO_ID="${SPREADSHEET_RL_HF_REPO_ID:-Spreadsheet-RL/Spreadsheet-RL}"
HF_LOCAL_DIR_BASE="$(expand_slurm_job_id_placeholder "${SPREADSHEET_RL_HF_LOCAL_DIR_BASE:-/tmp/spreadsheet_rl_data_${SLURM_JOB_ID:-$$}}")"
PREFETCH_MODEL="${SPREADSHEET_RL_PREFETCH_MODEL:-0}"
MODEL_REPO_ID="${SPREADSHEET_RL_MODEL_REPO_ID:-}"
MODEL_LOCAL_DIR_BASE="$(expand_slurm_job_id_placeholder "${SPREADSHEET_RL_MODEL_LOCAL_DIR_BASE:-/tmp/spreadsheet_rl_models_${SLURM_JOB_ID:-$$}}")"
SPREADSHEET_RL_USE_PREFETCHED_MODEL="${SPREADSHEET_RL_USE_PREFETCHED_MODEL:-0}"

WORKSPACES_DIR="$(expand_slurm_job_id_placeholder "${SPREADSHEET_RL_WORKSPACES_DIR:-${PROJECT_DIR}/slurm_workspaces/${SLURM_JOB_ID:-$$}}")"
RUN_DIR="$(expand_slurm_job_id_placeholder "${SPREADSHEET_RL_RUN_DIR:-/tmp/spreadsheet_rl_${SLURM_JOB_ID:-$$}}")"
RAY_LOGS_ROOT="$(expand_slurm_job_id_placeholder "${RAY_LOGS_ROOT:-${PROJECT_DIR}/slurm_logs/ray_${SLURM_JOB_ID:-$$}}")"

export RAY_DATA_HOME
export HF_HOME="${HF_HOME:-${RAY_DATA_HOME}/hf}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-${HF_HOME}/datasets}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-${HF_HUB_CACHE}}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${RAY_DATA_HOME}/.cache}"
export PROJECT_DIR
export MODEL_PATH
export GDRCOPY_HOME
export NVSHMEM_DIR
export MODULE_CUDNN
export CUDNN_HOME
export CUDNN_PATH
export SANDBOX_FUSION_SIF SANDBOX_FUSION_HOST SANDBOX_FUSION_PORT SANDBOX_FUSION_URL
export HF_REPO_ID HF_LOCAL_DIR_BASE PREFETCH_MODEL MODEL_REPO_ID MODEL_LOCAL_DIR_BASE SPREADSHEET_RL_USE_PREFETCHED_MODEL
export WORKSPACES_DIR RUN_DIR RAY_LOGS_ROOT

TRAIN_SH_INPUT="${1:-${SPREADSHEET_RL_CONFIG:-configs/qwen3-4b.sh}}"
shift || true
USER_OVERRIDES=("$@")
TRAIN_SH="$(resolve_existing_path "${TRAIN_SH_INPUT}" "${SLURM_SUBMIT_DIR:-}" "${PWD}" "${REPO_ROOT}" || true)"
if [[ -z "${TRAIN_SH}" ]]; then
    echo "Error: training script not found: ${TRAIN_SH_INPUT}" >&2
    exit 1
fi

NNODES="${SLURM_JOB_NUM_NODES:-${SLURM_NNODES:-1}}"
CPUS_PER_TASK="${SLURM_CPUS_PER_TASK:-1}"

detect_gpus_per_node() {
    local v=""
    if [[ -n "${SLURM_GPUS_ON_NODE:-}" ]]; then
        v="${SLURM_GPUS_ON_NODE}"
    elif [[ -n "${SLURM_GPUS_PER_NODE:-}" ]]; then
        v="${SLURM_GPUS_PER_NODE}"
    elif [[ -n "${SLURM_GPUS_PER_TASK:-}" ]]; then
        v="${SLURM_GPUS_PER_TASK}"
    fi

    if [[ -n "${v}" ]]; then
        if [[ "${v}" =~ ^([0-9]+,)+[0-9]+$ ]]; then
            awk -F',' '{print NF}' <<<"${v}"
            return 0
        fi
        grep -oE '[0-9]+' <<<"${v}" | head -n1
        return 0
    fi

    nvidia-smi -L 2>/dev/null | wc -l
}

GPUS_PER_NODE="$(detect_gpus_per_node)"
if [[ -z "${GPUS_PER_NODE}" || "${GPUS_PER_NODE}" -le 0 ]]; then
    echo "Error: failed to detect GPUs per node." >&2
    exit 1
fi

mapfile -t nodes_array < <(scontrol show hostnames "${SLURM_JOB_NODELIST}")
if [[ "${#nodes_array[@]}" -eq 0 ]]; then
    echo "Error: failed to resolve allocated Slurm nodes from SLURM_JOB_NODELIST=${SLURM_JOB_NODELIST:-<unset>}." >&2
    exit 1
fi
head_node="${nodes_array[0]}"

select_ip() {
    local ip_list="$1"
    local -a addrs=()
    read -ra addrs <<<"${ip_list}"

    # Prefer an IPv4 address if available.
    for ip in "${addrs[@]}"; do
        if [[ "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ && "${ip}" != "127.0.0.1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    # Prefer a non-link-local IPv6 address without an interface scope.
    for ip in "${addrs[@]}"; do
        if [[ "${ip}" == *:* && "${ip}" != fe80:* && "${ip}" != *%* && "${ip}" != "::1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    # Otherwise, fallback to the first non-empty address.
    for ip in "${addrs[@]}"; do
        if [[ -n "${ip}" && "${ip}" != "::1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    echo "${ip_list}"
}

get_head_raw_ip() {
    local current_node=""
    local raw_ip=""

    current_node="$(hostname -s)"
    if [[ "${current_node}" == "${head_node}" ]]; then
        hostname -I | tr -d '\n'
        return 0
    fi

    raw_ip="$(srun --mpi=none --overlap --nodes=1 --ntasks=1 -w "${head_node}" hostname -I 2>/dev/null | tr -d '\n')" || true
    if [[ -n "${raw_ip}" ]]; then
        echo "${raw_ip}"
        return 0
    fi

    getent ahostsv4 "${head_node}" | awk '{print $1; exit}'
}

head_raw_ip="$(get_head_raw_ip)"
head_node_ip="$(select_ip "${head_raw_ip}")"
if [[ -z "${head_node_ip}" ]]; then
    echo "Error: failed to determine Ray head IP for node ${head_node}." >&2
    exit 1
fi

RAY_PORT="${RAY_PORT:-6379}"
format_ray_address() {
    local host="$1"
    local port="$2"
    if [[ "${host}" == \[*\] ]]; then
        echo "${host}:${port}"
    elif [[ "${host}" == *:* ]]; then
        echo "[${host}]:${port}"
    else
        echo "${host}:${port}"
    fi
}

ip_head="$(format_ray_address "${head_node_ip}" "${RAY_PORT}")"

detect_socket_ifname() {
    local ip="$1"
    if ! command -v ip >/dev/null 2>&1; then
        return 0
    fi
    if [[ "${ip}" == *:* ]]; then
        ip -o -6 addr show | awk -v ip="${ip}" '$4 ~ ip {print $2; exit}'
    else
        ip -o -4 addr show | awk -v ip="${ip}" 'index($4, ip"/") == 1 {print $2; exit}'
    fi
}

SOCKET_IFNAME="${SOCKET_IFNAME:-$(detect_socket_ifname "${head_node_ip}")}"
if [[ -n "${SOCKET_IFNAME}" ]]; then
    export SOCKET_IFNAME
    export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-${SOCKET_IFNAME}}"
    export NCCL_SOCKET_IFNAME="${NCCL_SOCKET_IFNAME:-${SOCKET_IFNAME}}"
    export TP_SOCKET_IFNAME="${TP_SOCKET_IFNAME:-${SOCKET_IFNAME}}"
    echo "SOCKET_IFNAME=${SOCKET_IFNAME}"
    echo "GLOO_SOCKET_IFNAME=${GLOO_SOCKET_IFNAME}"
    echo "NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME}"
fi

mkdir -p "${RAY_DATA_HOME}" "${PROJECT_DIR}/slurm_logs" "${WORKSPACES_DIR}" "${WORKSPACES_DIR}/.locks" "${RAY_LOGS_ROOT}"
mkdir -p "${PROJECT_DIR}/outputs" "${PROJECT_DIR}/checkpoints" "${PROJECT_DIR}/wandb" "${PROJECT_DIR}/rollout_data" "${PROJECT_DIR}/validation_data"

if [[ ! -f "${SANDBOX_FUSION_SIF}" ]]; then
    echo "Error: SANDBOX_FUSION_SIF not found: ${SANDBOX_FUSION_SIF:-<unset>}" >&2
    echo "Set SANDBOX_FUSION_SIF to a valid .sif path." >&2
    exit 1
fi
if [[ -n "${VENV_DIR}" && ! -f "${VENV_DIR}/bin/activate" ]]; then
    echo "Error: Python venv activation script not found: ${VENV_DIR}/bin/activate" >&2
    echo "Set VENV_DIR to a training environment with verl dependencies (ray, hydra, torch, vllm, transformers)." >&2
    exit 1
fi

if [[ -n "${MODEL_PATH}" ]]; then
    resolved_model_path="$(resolve_existing_path "${MODEL_PATH}" "${SLURM_SUBMIT_DIR:-}" "${PWD}" "${REPO_ROOT}" || true)"
    if [[ -n "${resolved_model_path}" ]]; then
        MODEL_PATH="${resolved_model_path}"
    elif [[ "${MODEL_PATH}" == /* || "${MODEL_PATH}" == ./* || "${MODEL_PATH}" == ../* ]]; then
        echo "Error: MODEL_PATH does not exist: ${MODEL_PATH}" >&2
        exit 1
    elif [[ -z "${MODEL_REPO_ID}" ]]; then
        MODEL_REPO_ID="${MODEL_PATH}"
    fi
fi

echo "PROJECT_DIR=${PROJECT_DIR}"
echo "VERL_DIR=${VERL_DIR}"
echo "VENV_DIR=${VENV_DIR:-<module-env>}"
echo "RAY_DATA_HOME=${RAY_DATA_HOME}"
echo "RUN_DIR=${RUN_DIR}"
echo "WORKSPACES_DIR=${WORKSPACES_DIR}"
echo "HF_REPO_ID=${HF_REPO_ID}"
echo "MODEL_PATH=${MODEL_PATH:-<config default>}"
echo "MODEL_REPO_ID=${MODEL_REPO_ID}"
echo "PREFETCH_MODEL=${PREFETCH_MODEL}"
echo "TRAIN_SH=${TRAIN_SH}"
echo "NNODES=${NNODES}"
echo "GPUS_PER_NODE=${GPUS_PER_NODE}"
echo "Ray head: ${ip_head}"
echo "SandboxFusion URL: ${SANDBOX_FUSION_URL}"
echo "GDRCOPY_HOME=${GDRCOPY_HOME}"
echo "NVSHMEM_DIR=${NVSHMEM_DIR}"

job_token="${SLURM_JOB_ID:-$$}"
head_addr_file="${PROJECT_DIR}/slurm_logs/ray_head_addr_${job_token}.txt"
done_file="${PROJECT_DIR}/slurm_logs/ray_done_${job_token}.txt"
rm -f "${head_addr_file}" "${done_file}"
find "${PROJECT_DIR}/slurm_logs" -maxdepth 1 -type f -name "sandbox_fusion_ready_${SLURM_JOB_ID:-$$}_*" -delete || true

SRUN_LAUNCH_SCRIPT=$(cat <<'EOF'
set -euo pipefail
module load "$1"
module load "$2"
if [[ -n "${MODULE_CUDNN:-}" ]]; then
    module load "${MODULE_CUDNN}"
fi
VENV_DIR_ARG="$3"
if [[ -n "${VENV_DIR_ARG}" ]]; then
    source "${VENV_DIR_ARG}/bin/activate"
fi
cd "$4"

RAY_PORT="$5"
CPUS_PER_TASK="$6"
GPUS_PER_NODE="$7"
TRAIN_SH="$8"
HEAD_ADDR_FILE="$9"
DONE_FILE="${10}"
EXPECTED_NODES="${11}"
shift 11
USER_OVERRIDES=("$@")

canonicalize_existing_path() {
    local path="$1"
    local dir=""
    dir="$(cd "$(dirname "${path}")" && pwd -P)"
    printf '%s/%s\n' "${dir}" "$(basename "${path}")"
}

export CC=/opt/cray/pe/gcc-native/12/bin/gcc
export CXX=/opt/cray/pe/gcc-native/12/bin/g++
export TRITON_CC="$CC"
export FI_PROVIDER="${FI_PROVIDER:-cxi}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-9.0}"
export GDRCOPY_HOME="${GDRCOPY_HOME:-${HOME}/.local/gdrcopy/2.5.1}"
export NVSHMEM_DIR="${NVSHMEM_DIR:-/sw/user/python/miniforge3-pytorch-2.10.0/lib/python3.12/site-packages/nvidia/nvshmem}"
cudnn_root="${CUDNN_HOME:-${CUDNN_PATH:-}}"
if [[ -n "${cudnn_root}" ]]; then
    export CUDNN_HOME="${CUDNN_HOME:-${cudnn_root}}"
    export CUDNN_PATH="${CUDNN_PATH:-${cudnn_root}}"
    if [[ -d "${cudnn_root}/lib" ]]; then
        export LD_LIBRARY_PATH="${cudnn_root}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    fi
    if [[ -d "${cudnn_root}/lib64" ]]; then
        export LD_LIBRARY_PATH="${cudnn_root}/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    fi
fi
if [[ -d "${GDRCOPY_HOME}/lib" ]]; then
    export LD_LIBRARY_PATH="${GDRCOPY_HOME}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi
if [[ -d "${NVSHMEM_DIR}/lib" ]]; then
    export LD_LIBRARY_PATH="${NVSHMEM_DIR}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

node_name="${SLURMD_NODENAME:-$(hostname -s)}"
local_cache_root="/tmp/verl_cache_${SLURM_JOB_ID:-$$}_${node_name}"
mkdir -p "${local_cache_root}"
export FLASH_ATTENTION_HOME="${local_cache_root}"
export FLASHINFER_WORKSPACE_BASE="${local_cache_root}"
export TRITON_HOME="${local_cache_root}"
export TRITON_CACHE_DIR="${local_cache_root}/.triton/cache"
export TRITON_DUMP_DIR="${local_cache_root}/.triton/dump"
export TRITON_OVERRIDE_DIR="${local_cache_root}/.triton/override"
export TORCH_EXTENSIONS_DIR="${local_cache_root}/torch_extensions"
export TORCHINDUCTOR_CACHE_DIR="${local_cache_root}/torch_inductor"
export XDG_CACHE_HOME="${local_cache_root}/xdg_cache"
export PYTHONPYCACHEPREFIX="${local_cache_root}/pycache"
export CUDA_CACHE_PATH="${local_cache_root}/nv_cache"
export TMPDIR="${local_cache_root}/tmp"
mkdir -p "${TMPDIR}"

ray_tmp_dir="/tmp/ray_${SLURM_JOB_ID:-$$}"
mkdir -p "${ray_tmp_dir}"
ulimit -n 65535 >/dev/null 2>&1 || true

if ! command -v ray >/dev/null 2>&1; then
    echo "[node:${node_name}] ERROR: ray not found." >&2
    exit 1
fi
if ! python3 - <<PY
import importlib.util
required = ["cachetools", "hydra", "openpyxl", "torch", "transformers", "vllm"]
missing = [m for m in required if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit("missing_python_dependencies:" + ",".join(missing))
PY
then
    echo "[node:${node_name}] ERROR: Python env missing required deps (cachetools, hydra, openpyxl, torch, transformers, vllm)." >&2
    echo "[node:${node_name}]       Set VENV_DIR to a training environment with verl dependencies." >&2
    exit 1
fi
if ! command -v uvx >/dev/null 2>&1; then
    echo "[node:${node_name}] ERROR: uvx not found." >&2
    exit 1
fi
if ! command -v apptainer >/dev/null 2>&1; then
    echo "[node:${node_name}] ERROR: apptainer not found." >&2
    exit 1
fi

ray stop -f >/dev/null 2>&1 || true
sandbox_pid=""
sandbox_log=""
cleanup_node() {
    if [[ -n "${sandbox_pid}" ]]; then
        kill "${sandbox_pid}" >/dev/null 2>&1 || true
        wait "${sandbox_pid}" >/dev/null 2>&1 || true
    fi
    ray stop -f >/dev/null 2>&1 || true
    if [[ "${SLURM_PROCID:-0}" == "0" ]]; then
        touch "${DONE_FILE}" >/dev/null 2>&1 || true
    fi
}
trap cleanup_node EXIT INT TERM

hf_local_dir="${HF_LOCAL_DIR_BASE}_${node_name}"
rm -rf "${hf_local_dir}"
mkdir -p "${hf_local_dir}"
if ! uvx hf download "${HF_REPO_ID}" --repo-type dataset --local-dir "${hf_local_dir}"; then
    echo "[node:${node_name}] ERROR: failed to download dataset ${HF_REPO_ID}" >&2
    exit 1
fi

if [[ "${PREFETCH_MODEL:-0}" == "1" && -n "${MODEL_REPO_ID:-}" ]]; then
    if [[ "${SPREADSHEET_RL_USE_PREFETCHED_MODEL:-0}" == "1" && "${EXPECTED_NODES}" -gt 1 ]]; then
        echo "[node:${node_name}] ERROR: SPREADSHEET_RL_USE_PREFETCHED_MODEL=1 is only supported for single-node runs." >&2
        echo "[node:${node_name}]        Use a shared model path via MODEL_PATH/MODEL_REPO_ID for multi-node runs." >&2
        exit 1
    fi
    model_local_dir="${MODEL_LOCAL_DIR_BASE}/${MODEL_REPO_ID//\//__}_${node_name}"
    rm -rf "${model_local_dir}"
    mkdir -p "${model_local_dir}"
    if ! uvx hf download "${MODEL_REPO_ID}" --repo-type model --local-dir "${model_local_dir}"; then
        echo "[node:${node_name}] ERROR: failed to prefetch model ${MODEL_REPO_ID}" >&2
        exit 1
    fi
    if [[ "${SPREADSHEET_RL_USE_PREFETCHED_MODEL:-0}" == "1" ]]; then
        export MODEL_PATH="${model_local_dir}"
    fi
fi

data_dir="${hf_local_dir}"
if [[ -d "${data_dir}/data" ]] && compgen -G "${data_dir}/data/*.parquet" >/dev/null; then
    data_dir="${data_dir}/data"
fi
if [[ -f "${data_dir}/spreadsheets.zip" ]] && [[ ! -d "${data_dir}/excelforum" && ! -d "${data_dir}/spreadsheetbench" && ! -d "${data_dir}/domain" ]]; then
    if ! command -v unzip >/dev/null 2>&1; then
        echo "[node:${node_name}] ERROR: unzip not found but spreadsheets.zip exists." >&2
        exit 1
    fi
    rm -rf "${data_dir}/excelforum" "${data_dir}/spreadsheetbench" "${data_dir}/domain"
    unzip -q "${data_dir}/spreadsheets.zip" -d "${data_dir}"
fi

resolve_data_file() {
    local input_path="$1"
    local default_rel="$2"
    local resolved=""

    if [[ -n "${input_path}" ]]; then
        if [[ "${input_path}" == /* ]]; then
            resolved="${input_path}"
        else
            resolved="${data_dir}/${input_path}"
        fi
    else
        resolved="${data_dir}/${default_rel}"
    fi

    if [[ ! -s "${resolved}" ]]; then
        echo ""
        return 1
    fi
    canonicalize_existing_path "${resolved}"
}

resolved_train_file="$(resolve_data_file "${TRAIN_FILE:-}" "train_hermes.parquet" || true)"
if [[ -z "${resolved_train_file}" ]]; then
    echo "[node:${node_name}] ERROR: missing training parquet under ${data_dir}: ${TRAIN_FILE:-train_hermes.parquet}" >&2
    echo "[node:${node_name}]        The default configs/qwen3-4b.sh launcher uses the Hermes-format training split." >&2
    exit 1
fi
resolved_test_file="$(resolve_data_file "${TEST_FILE:-}" "test_hermes.parquet" || true)"
if [[ -z "${resolved_test_file}" ]]; then
    echo "[node:${node_name}] ERROR: missing validation parquet under ${data_dir}: ${TEST_FILE:-test_hermes.parquet}" >&2
    echo "[node:${node_name}]        The default configs/qwen3-4b.sh launcher uses the Hermes-format validation split." >&2
    exit 1
fi

mkdir -p "${WORKSPACES_DIR}" "${WORKSPACES_DIR}/.locks"
mkdir -p "${RUN_DIR}"
ln -sfn "${PROJECT_DIR}/verl" "${RUN_DIR}/verl"
ln -sfn "${PROJECT_DIR}/verl/examples" "${RUN_DIR}/examples"
ln -sfn "${PROJECT_DIR}/configs" "${RUN_DIR}/configs"
if [[ -d "${PROJECT_DIR}/model_weights" ]]; then
    ln -sfn "${PROJECT_DIR}/model_weights" "${RUN_DIR}/model_weights"
fi
ln -sfn "${PROJECT_DIR}/outputs" "${RUN_DIR}/outputs"
ln -sfn "${PROJECT_DIR}/checkpoints" "${RUN_DIR}/checkpoints"
ln -sfn "${PROJECT_DIR}/wandb" "${RUN_DIR}/wandb"
ln -sfn "${PROJECT_DIR}/rollout_data" "${RUN_DIR}/rollout_data"
ln -sfn "${PROJECT_DIR}/validation_data" "${RUN_DIR}/validation_data"
ln -sfn "${PROJECT_DIR}/slurm_logs" "${RUN_DIR}/slurm_logs"

rm -rf "${RUN_DIR}/data"
mkdir -p "${RUN_DIR}/data"
ln -sfn "${resolved_train_file}" "${RUN_DIR}/data/$(basename "${resolved_train_file}")"
ln -sfn "${resolved_test_file}" "${RUN_DIR}/data/$(basename "${resolved_test_file}")"
for parquet in "${data_dir}"/*.parquet; do
    if [[ -f "${parquet}" ]]; then
        ln -sfn "${parquet}" "${RUN_DIR}/data/$(basename "${parquet}")"
    fi
done
for d in excelforum spreadsheetbench domain; do
    if [[ -d "${data_dir}/${d}" ]]; then
        ln -sfn "${data_dir}/${d}" "${RUN_DIR}/data/${d}"
    fi
done
ln -sfn "${WORKSPACES_DIR}" "${RUN_DIR}/data/_workspaces"

export SPREADSHEET_RL_DATA_ROOT="${RUN_DIR}/data"
export SPREADSHEET_RL_VERL_CONFIG_DIR="${RUN_DIR}/verl/verl/trainer/config"
export TRAIN_FILE="${RUN_DIR}/data/$(basename "${resolved_train_file}")"
export TEST_FILE="${RUN_DIR}/data/$(basename "${resolved_test_file}")"
export SANDBOX_FUSION_URL

sandbox_log="${PROJECT_DIR}/slurm_logs/sandbox_fusion_${SLURM_JOB_ID:-$$}_${node_name}.log"
sandbox_ready_file="${PROJECT_DIR}/slurm_logs/sandbox_fusion_ready_${SLURM_JOB_ID:-$$}_${node_name}"
rm -f "${sandbox_ready_file}"
APPTAINERENV_SANDBOX_CONFIG="hpc" \
APPTAINERENV_SANDBOX_FUSION_HOST="${SANDBOX_FUSION_HOST}" \
APPTAINERENV_SANDBOX_FUSION_PORT="${SANDBOX_FUSION_PORT}" \
APPTAINERENV_SANDBOX_DATA_ROOT="/opt/sandboxfusion/data" \
    apptainer run --cleanenv \
        --bind "${RUN_DIR}/data:/opt/sandboxfusion/data" \
        --bind "${WORKSPACES_DIR}:/opt/sandboxfusion/data/_workspaces" \
        "${SANDBOX_FUSION_SIF}" >"${sandbox_log}" 2>&1 &
sandbox_pid=$!

sandbox_ready=0
for _ in $(seq 1 120); do
    if ! kill -0 "${sandbox_pid}" >/dev/null 2>&1; then
        echo "[node:${node_name}] ERROR: SandboxFusion exited early." >&2
        tail -n 200 "${sandbox_log}" >&2 || true
        exit 1
    fi
    if (echo >"/dev/tcp/${SANDBOX_FUSION_HOST}/${SANDBOX_FUSION_PORT}") >/dev/null 2>&1; then
        sandbox_ready=1
        break
    fi
    sleep 1
done
if [[ "${sandbox_ready}" -ne 1 ]]; then
    echo "[node:${node_name}] ERROR: SandboxFusion did not become ready on ${SANDBOX_FUSION_HOST}:${SANDBOX_FUSION_PORT}" >&2
    tail -n 200 "${sandbox_log}" >&2 || true
    exit 1
fi
touch "${sandbox_ready_file}"

select_ip() {
    local ip_list="$1"
    local -a addrs=()
    read -ra addrs <<<"${ip_list}"

    for ip in "${addrs[@]}"; do
        if [[ "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ && "${ip}" != "127.0.0.1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    for ip in "${addrs[@]}"; do
        if [[ "${ip}" == *:* && "${ip}" != fe80:* && "${ip}" != *%* && "${ip}" != "::1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    for ip in "${addrs[@]}"; do
        if [[ -n "${ip}" && "${ip}" != "::1" ]]; then
            echo "${ip}"
            return 0
        fi
    done

    echo "${ip_list}"
}

format_ray_address() {
    local host="$1"
    local port="$2"
    if [[ "${host}" == \[*\] ]]; then
        echo "${host}:${port}"
    elif [[ "${host}" == *:* ]]; then
        echo "[${host}]:${port}"
    else
        echo "${host}:${port}"
    fi
}

node_raw_ip="$(hostname -I | tr -d "\n")"
node_ip="$(select_ip "${node_raw_ip}")"
if [[ -z "${node_ip}" ]]; then
    echo "[node:${node_name}] ERROR: failed to determine node IP." >&2
    exit 1
fi

if [[ "${SLURM_PROCID}" == "0" ]]; then
    head_addr="$(format_ray_address "${node_ip}" "${RAY_PORT}")"
    echo "Ray head: ${head_addr} (node_ip=${node_ip})"
    ray start --head --node-ip-address="${node_ip}" --port="${RAY_PORT}" --temp-dir="${ray_tmp_dir}" \
        --num-cpus="${CPUS_PER_TASK}" --num-gpus="${GPUS_PER_NODE}" --disable-usage-stats
    echo "${head_addr}" > "${HEAD_ADDR_FILE}"

    ready_count=0
    for _ in $(seq 1 180); do
        ready_count="$(find "${PROJECT_DIR}/slurm_logs" -maxdepth 1 -type f -name "sandbox_fusion_ready_${SLURM_JOB_ID:-$$}_*" | wc -l | tr -d " ")"
        if [[ "${ready_count}" -ge "${EXPECTED_NODES}" ]]; then
            break
        fi
        sleep 1
    done
    if [[ "${ready_count}" -lt "${EXPECTED_NODES}" ]]; then
        echo "[head:${node_name}] ERROR: expected ${EXPECTED_NODES} sandbox ready markers, found ${ready_count}" >&2
        exit 1
    fi

    export EXPECTED_NODES
    ray_ready=0
    for _ in $(seq 1 30); do
        if python3 - <<PY
import os
import ray

expected = int(os.environ["EXPECTED_NODES"])
ray.init(address="auto")
nodes = ray.nodes()
alive = [n for n in nodes if n.get("Alive", False)]
print(f"Ray nodes: alive={len(alive)} total={len(nodes)} expected={expected}")
ray.shutdown()
if len(alive) != expected:
    raise SystemExit(2)
PY
        then
            ray_ready=1
            break
        fi
        sleep 2
    done
    if [[ "${ray_ready}" -ne 1 ]]; then
        echo "[head:${node_name}] ERROR: Ray cluster did not reach ${EXPECTED_NODES} alive nodes." >&2
        exit 1
    fi

    cd "${RUN_DIR}/verl"
    echo "Launching training on ${HOSTNAME} (cwd=${RUN_DIR}/verl)"
    echo "Training PROJECT_DIR=${RUN_DIR}"
    if [[ -n "${TRAIN_FILE:-}" ]]; then
        echo "TRAIN_FILE override=${TRAIN_FILE}"
    fi
    if [[ -n "${TEST_FILE:-}" ]]; then
        echo "TEST_FILE override=${TEST_FILE}"
    fi
    TOTAL_GPUS="$((GPUS_PER_NODE * EXPECTED_NODES))"
    BASE_OVERRIDES=(
        "actor_rollout_ref.actor.fsdp_config.fsdp_size=${TOTAL_GPUS}"
    )

    PROJECT_DIR="${RUN_DIR}" bash "${TRAIN_SH}" \
        "+ray_kwargs.ray_init.address=auto" \
        "trainer.n_gpus_per_node=${GPUS_PER_NODE}" \
        "trainer.nnodes=${EXPECTED_NODES}" \
        "${BASE_OVERRIDES[@]}" \
        "${USER_OVERRIDES[@]}"

    touch "${DONE_FILE}"
else
    head_waited=0
    while [[ ! -s "${HEAD_ADDR_FILE}" ]]; do
        if [[ -f "${DONE_FILE}" ]]; then
            echo "[node:${node_name}] ERROR: Ray head address never became available (DONE_FILE present)." >&2
            exit 1
        fi
        sleep 1
        head_waited="$((head_waited + 1))"
        if [[ "${head_waited}" -ge 300 ]]; then
            echo "[node:${node_name}] ERROR: timed out waiting for Ray head address file: ${HEAD_ADDR_FILE}" >&2
            exit 1
        fi
    done
    head_addr="$(cat "${HEAD_ADDR_FILE}")"
    echo "Ray worker ${SLURM_PROCID}: connecting to ${head_addr} (node_ip=${node_ip})"
    ray_joined=0
    for _ in $(seq 1 30); do
        if ray start --address="${head_addr}" --node-ip-address="${node_ip}" --temp-dir="${ray_tmp_dir}" \
            --num-cpus="${CPUS_PER_TASK}" --num-gpus="${GPUS_PER_NODE}" --disable-usage-stats; then
            ray_joined=1
            break
        fi
        ray stop -f >/dev/null 2>&1 || true
        sleep 2
    done
    if [[ "${ray_joined}" -ne 1 ]]; then
        echo "[node:${node_name}] ERROR: failed to join Ray cluster at ${head_addr}" >&2
        exit 1
    fi

    while [[ ! -f "${DONE_FILE}" ]]; do
        if ! kill -0 "${sandbox_pid}" >/dev/null 2>&1; then
            echo "[node:${node_name}] ERROR: SandboxFusion exited before training finished." >&2
            tail -n 200 "${sandbox_log}" >&2 || true
            exit 1
        fi
        sleep 5
    done
fi
EOF
)

echo "Launching Ray + SandboxFusion + training via a single Slurm step"
srun --mpi=none --kill-on-bad-exit=1 --nodes="${NNODES}" --ntasks="${NNODES}" --ntasks-per-node=1 bash -lc "${SRUN_LAUNCH_SCRIPT}" _ "${MODULE_PYTHON}" "${MODULE_NCCL_OFI}" "${VENV_DIR}" "${VERL_DIR}" "${RAY_PORT}" "${CPUS_PER_TASK}" "${GPUS_PER_NODE}" \
    "${TRAIN_SH}" "${head_addr_file}" "${done_file}" "${NNODES}" \
    "${USER_OVERRIDES[@]}"
