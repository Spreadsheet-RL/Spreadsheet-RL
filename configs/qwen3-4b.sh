#!/bin/bash
set -euo pipefail
set -x

project_name='spreadsheet-rl'
exp_name='qwen3-4b'
gen_tp=1
sp_size=1
ENGINE=vllm
if [[ $# -gt 0 ]]; then
    case "$1" in
        vllm|vllm_omni|sglang|trtllm)
            ENGINE="$1"
            shift
            ;;
    esac
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR=${PROJECT_DIR:-"$(cd "${SCRIPT_DIR}/.." && pwd)"}
export RAY_DATA_HOME=${RAY_DATA_HOME:-"${PROJECT_DIR}/verl_data"}
MODEL_PATH=${MODEL_PATH:-"${SPREADSHEET_RL_MODEL_REPO_ID:-${MODEL_REPO_ID:-Qwen/Qwen3-4B-Thinking-2507}}"}
CKPTS_DIR=${CKPTS_DIR:-"${PROJECT_DIR}/checkpoints/${project_name}/${exp_name}"}
SPREADSHEET_RL_DATA_ROOT=${SPREADSHEET_RL_DATA_ROOT:-"${PROJECT_DIR}/data"}
TRAIN_FILE=${TRAIN_FILE:-"${SPREADSHEET_RL_DATA_ROOT}/train_hermes.parquet"}
TEST_FILE=${TEST_FILE:-"${SPREADSHEET_RL_DATA_ROOT}/test_hermes.parquet"}
CONFIG_PATH=${CONFIG_PATH:-"${PROJECT_DIR}/configs"}
TOOL_CONFIG_PATH=${TOOL_CONFIG_PATH:-"${PROJECT_DIR}/configs/tool/spreadsheet_tools.yaml"}
LOG_DIR=${LOG_DIR:-"${PROJECT_DIR}/logs"}

export SPREADSHEET_RL_DATA_ROOT
export SPREADSHEET_RL_VERL_CONFIG_DIR=${SPREADSHEET_RL_VERL_CONFIG_DIR:-"${PROJECT_DIR}/verl/verl/trainer/config"}
export PYTHONPATH="${PROJECT_DIR}/verl${PYTHONPATH:+:${PYTHONPATH}}"

: "${SPREADSHEET_RL_REWARD_URL:?Set SPREADSHEET_RL_REWARD_URL to the reward submit endpoint.}"
: "${SPREADSHEET_RL_RECALC_URL:?Set SPREADSHEET_RL_RECALC_URL to the recalculation endpoint.}"

start_time=$(date +%Y%m%d)_$(date +%H%M%S)

mkdir -p "${LOG_DIR}"
python3 -m verl.trainer.main_ppo \
    --config-path="${CONFIG_PATH}" \
    --config-name='spreadsheet_rl_multiturn_grpo' \
    algorithm.adv_estimator=grpo \
    data.train_files="${TRAIN_FILE}" \
    data.val_files="${TEST_FILE}" \
    data.train_batch_size=128 \
    data.val_batch_size=1400 \
    data.max_response_length=24576 \
    data.filter_overlong_prompts=True \
    data.truncation='error' \
    data.return_raw_chat=True \
    actor_rollout_ref.model.path="${MODEL_PATH}" \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.model.use_fused_kernels=True \
    actor_rollout_ref.model.fused_kernel_options.impl_backend="triton" \
    +actor_rollout_ref.model.override_config.attn_implementation="flash_attention_3" \
    actor_rollout_ref.actor.optim.lr=2e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=64 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.use_torch_compile=False \
    actor_rollout_ref.actor.strategy=fsdp2 \
    actor_rollout_ref.ref.strategy=fsdp2 \
    actor_rollout_ref.actor.fsdp_config.fsdp_size=4 \
    actor_rollout_ref.actor.fsdp_config.reshard_after_forward=True \
    actor_rollout_ref.ref.fsdp_config.reshard_after_forward=True \
    actor_rollout_ref.actor.fsdp_config.entropy_checkpointing=True \
    actor_rollout_ref.actor.entropy_from_logits_with_chunking=True \
    actor_rollout_ref.actor.use_dynamic_bsz=True \
    actor_rollout_ref.actor.ulysses_sequence_parallel_size=${sp_size} \
    actor_rollout_ref.actor.fsdp_config.offload_policy=False \
    actor_rollout_ref.actor.fsdp_config.param_offload=True \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \
    actor_rollout_ref.ref.log_prob_use_dynamic_bsz=True \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    actor_rollout_ref.ref.entropy_from_logits_with_chunking=True \
    actor_rollout_ref.ref.ulysses_sequence_parallel_size=${sp_size} \
    actor_rollout_ref.ref.use_torch_compile=False \
    actor_rollout_ref.ref.fsdp_config.offload_policy=True \
    actor_rollout_ref.rollout.name=${ENGINE} \
    actor_rollout_ref.rollout.ignore_eos=False \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.rollout.log_prob_use_dynamic_bsz=True \
    actor_rollout_ref.rollout.tensor_model_parallel_size=${gen_tp} \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.85 \
    actor_rollout_ref.rollout.max_model_len=28672 \
    actor_rollout_ref.rollout.n=8 \
    actor_rollout_ref.rollout.top_k=20 \
    actor_rollout_ref.rollout.top_p=0.95 \
    actor_rollout_ref.rollout.temperature=0.6 \
    actor_rollout_ref.rollout.val_kwargs.n=1 \
    actor_rollout_ref.rollout.val_kwargs.top_k=20 \
    actor_rollout_ref.rollout.val_kwargs.top_p=0.95 \
    actor_rollout_ref.rollout.val_kwargs.temperature=0.0 \
    actor_rollout_ref.rollout.val_kwargs.do_sample=False \
    actor_rollout_ref.rollout.enable_chunked_prefill=True \
    actor_rollout_ref.rollout.max_num_batched_tokens=24576 \
    actor_rollout_ref.rollout.free_cache_engine=True \
    actor_rollout_ref.rollout.enforce_eager=False \
    actor_rollout_ref.rollout.enable_prefix_caching=False \
    actor_rollout_ref.rollout.checkpoint_engine.update_weights_bucket_megabytes=1024 \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="${TOOL_CONFIG_PATH}" \
    actor_rollout_ref.rollout.multi_turn.format=hermes \
    actor_rollout_ref.rollout.multi_turn.max_user_turns=15 \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=15 \
    actor_rollout_ref.rollout.agent.default_agent_loop=tool_agent \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=32768 \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=65536 \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=65536 \
    algorithm.use_kl_in_reward=False \
    +reward.max_concurrent=2 \
    +reward.timeout=10800 \
    trainer.critic_warmup=0 \
    trainer.logger='["console", "wandb"]' \
    trainer.project_name="${project_name}" \
    trainer.experiment_name="${exp_name}" \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.balance_batch=False \
    trainer.default_local_dir="${CKPTS_DIR}" \
    trainer.rollout_data_dir="$PROJECT_DIR/rollout_data/${project_name}/${exp_name}" \
    trainer.validation_data_dir="$PROJECT_DIR/validation_data/${project_name}/${exp_name}" \
    trainer.val_before_train=True \
    trainer.save_freq=5 \
    trainer.test_freq=15 \
    trainer.total_epochs=2 "$@" 2>&1 | tee "${LOG_DIR}/${exp_name}-${start_time}.log"
