from pathlib import Path
import subprocess

from omegaconf import OmegaConf
import pytest

from resources_servers.spreadsheet.packaging.prepare_existing_launcher import (
    apply_updates,
    edit_launcher,
    gpu_layout,
    patch_yaml,
    prepare_values,
    replace_atomic,
    write_private,
)


def test_yaml_migration_preserves_full_training_schedule_and_unrelated_comments():
    raw = """defaults: /fixture/base.yaml
# Keep original schedule
grpo:
  num_prompts_per_step: 128
  num_generations_per_prompt: 8
  max_num_steps: 676767
policy:
  megatron_cfg:
    tensor_model_parallel_size: 1
  generation:
    max_new_tokens: 32638
    vllm_cfg:
      http_server_serving_chat_kwargs:
        tool_parser: qwen3_coder
data:
  shuffle: true
  train:
    - data_path: /fixture/old.jsonl
env:
  should_use_nemo_gym: true
  nemo_gym:
    policy_model:
      responses_api_models:
        vllm_model:
          num_workers: 16
    unused_judge:
      resources_servers:
        math_with_judge: {}
token_ids:
  eos: 151643
"""
    base = OmegaConf.to_container(OmegaConf.create(raw), resolve=False)
    changes = prepare_values(
        base,
        Path("/fixture/gym"),
        ["model.yaml", "spreadsheet.yaml", "/private/gym.yaml"],
        "/fixture/new.jsonl",
        {"tool_parser": "hermes", "eos": 151645},
    )
    for path, value in changes:
        if path[:2] == ["data", "train"]:
            raw = patch_yaml(raw, ["data", "train"], [{"data_path": value}])
        else:
            raw = patch_yaml(raw, path, {"_override_": True, **value} if path == ["env"] else value)
    result = OmegaConf.create(raw)
    assert "# Keep original schedule" in raw
    assert result.grpo == OmegaConf.create(base["grpo"])
    assert result.policy.generation.max_new_tokens == 32638
    assert result.env.nemo_gym.policy_model.responses_api_models.vllm_model.num_workers == 16
    assert result.token_ids.eos == 151645
    assert result.policy.generation.vllm_cfg.http_server_serving_chat_kwargs.tool_parser == "hermes"
    assert result.policy.megatron_cfg.sequence_parallel is False
    assert "unused_judge" not in result.env.nemo_gym
    assert result.data.train[0].data_path == "/fixture/new.jsonl" and result.data.shuffle
    assert "unused_judge" in base["env"]["nemo_gym"]


@pytest.mark.parametrize(
    "selected",
    [None, {"indices": [0, 1, 2, 3, 4, 5], "uuids": ["GPU-" + str(i) for i in range(6)], "hostname": "train-node"}],
)
def test_existing_launcher_uses_short_ipc_and_preserves_activation_and_full_entrypoint(tmp_path, selected):
    text = """#!/bin/bash
SCRIPT_DIR=/fixture
CONFIG_PATH="${SCRIPT_DIR}/qwen_test.yaml"
source "${NEMO_RL_UTILS_DIR}/use_custom_opt.sh" "$NEMO_RL_BASE_DIR" /fixture/nltk
bash "${NEMO_RL_UTILS_DIR}/copy_gym_venvs.sh" "${CONFIG_PATH}"
{
    python "${NEMO_RL_BASE_DIR}/examples/nemo_gym/run_grpo_nemo_gym.py" --config "${CONFIG_PATH}"
} 2>&1 | tee "${RUN_LOG_DIR}/main.txt"
printf "Training ended.\\n"
"""
    edited = edit_launcher(
        text,
        Path("/fixture/qwen_test.yaml"),
        Path("/fixture/smoke_driver.py"),
        Path("/fixture/gym"),
        ["127.0.0.1", "172.29.185.171"],
        selected,
    )
    subprocess.run(["bash", "-n"], input=edited, text=True, check=True)
    assert '--ray-root "${SPREADSHEET_IPC_ROOT}"' in edited
    assert "/tmp/spreadsheet-rl.XXXXXXXX" in edited
    assert 'source "${NEMO_RL_UTILS_DIR}/use_custom_opt.sh"' in edited
    assert "copy_gym_venvs.sh" in edited
    assert "${PIPESTATUS[0]}" in edited and edited.endswith('exit "${spreadsheet_train_rc}"\n')
    assert "max_num_steps" not in edited
    if selected:
        assert "--gpu-indices '0,1,2,3,4,5'" in edited
        assert "--gpu-uuids 'GPU-0,GPU-1,GPU-2,GPU-3,GPU-4,GPU-5'" in edited
        assert "--expected-hostname 'train-node'" in edited
    else:
        assert "--gpu-indices" not in edited
    with pytest.raises(ValueError, match="already patched"):
        edit_launcher(edited, Path("/fixture/qwen_test.yaml"), Path("/fixture/driver.py"), Path("/fixture/gym"), [])


def test_six_gpu_profile_only_changes_resource_counts_and_preserves_unresolved_judges():
    base = OmegaConf.create(
        {
            "cluster": {"num_nodes": 1, "gpus_per_node": 2},
            "grpo": {"num_prompts_per_step": 128, "num_generations_per_prompt": 8, "max_num_steps": 676767},
            "policy": {
                "megatron_cfg": {"tensor_model_parallel_size": 1},
                "generation": {
                    "max_new_tokens": 32638,
                    "vllm_cfg": {"tensor_parallel_size": 1},
                    "colocated": {"enabled": False, "resources": {"gpus_per_node": 1, "num_nodes": 1}},
                },
            },
            "env": {"unused_judge": "${missing_judge_url}"},
        }
    )
    original = OmegaConf.to_container(base, resolve=False)
    changes, layout = gpu_layout(base, {"indices": list(range(6))}, 2)
    assert layout == {"train_gpus": 4, "rollout_gpus": 2, "data_parallel_size": 4}
    for path, value in changes:
        OmegaConf.update(base, ".".join(path), value)
    assert base.cluster.gpus_per_node == 6
    assert base.policy.generation.colocated.resources.gpus_per_node == 2
    result = OmegaConf.to_container(base, resolve=False)
    original["cluster"]["gpus_per_node"] = 6
    original["policy"]["generation"]["colocated"]["resources"]["gpus_per_node"] = 2
    assert result == original


@pytest.mark.parametrize(
    "rollout,train_tp,rollout_tp,opd",
    [(0, 1, 1, False), (6, 1, 1, False), (2, 3, 1, False), (2, 1, 3, False), (2, 1, 1, True)],
)
def test_invalid_resource_splits_or_opd_are_rejected(rollout, train_tp, rollout_tp, opd):
    base = {
        "cluster": {"num_nodes": 1},
        "on_policy_distillation": {"enabled": opd},
        "policy": {
            "megatron_cfg": {"tensor_model_parallel_size": train_tp},
            "generation": {"vllm_cfg": {"tensor_parallel_size": rollout_tp}},
        },
    }
    with pytest.raises(ValueError):
        gpu_layout(base, {"indices": list(range(6))}, rollout)


def test_atomic_replacement_and_private_backup_are_restorable(tmp_path):
    path = tmp_path / "launcher.sh"
    path.write_bytes(b"original")
    path.chmod(0o755)
    backup = tmp_path / "backup"
    write_private(backup, path.read_bytes())
    replace_atomic(path, b"changed")
    assert path.stat().st_mode & 0o777 == 0o755
    assert backup.stat().st_mode & 0o777 == 0o600
    replace_atomic(path, backup.read_bytes())
    assert path.read_bytes() == b"original"
    with pytest.raises(FileExistsError):
        write_private(backup, b"overwrite")


def test_preparation_creates_separate_launcher_without_writing_original(tmp_path):
    config, original_launcher, generated = (
        tmp_path / name for name in ("qwen_test.yaml", "singlenode_main.sh", "singlenode_spreadsheet.sh")
    )
    config.write_bytes(b"old config")
    original_launcher.write_bytes(b"original launcher")
    before = {path: path.read_bytes() for path in (config, original_launcher)}
    original_mode = original_launcher.stat().st_mode
    apply_updates(config, original_launcher, generated, before, b"spreadsheet config", b"derived launcher")
    assert config.read_bytes() == b"spreadsheet config"
    assert generated.read_bytes() == b"derived launcher"
    assert original_launcher.read_bytes() == before[original_launcher]
    assert original_launcher.stat().st_mode == original_mode
    with pytest.raises(FileExistsError):
        apply_updates(config, original_launcher, generated, before, b"other config", b"other launcher")
    assert original_launcher.read_bytes() == b"original launcher"


def test_failed_config_write_removes_only_generated_launcher_and_preserves_original(tmp_path, monkeypatch):
    from resources_servers.spreadsheet.packaging import prepare_existing_launcher as module

    config, original_launcher, generated = (
        tmp_path / name for name in ("qwen_test.yaml", "singlenode_main.sh", "singlenode_spreadsheet.sh")
    )
    config.write_bytes(b"old config")
    original_launcher.write_bytes(b"original launcher")
    before = {path: path.read_bytes() for path in (config, original_launcher)}
    monkeypatch.setattr(module, "replace_atomic", lambda *args: (_ for _ in ()).throw(OSError("write refused")))
    with pytest.raises(OSError, match="write refused"):
        apply_updates(config, original_launcher, generated, before, b"new config", b"derived launcher")
    assert not generated.exists()
    assert config.read_bytes() == b"old config"
    assert original_launcher.read_bytes() == b"original launcher"
