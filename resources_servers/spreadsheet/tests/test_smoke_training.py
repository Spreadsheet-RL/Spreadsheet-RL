import json
from pathlib import Path

from omegaconf import OmegaConf

from resources_servers.spreadsheet.packaging.smoke_training import (
    ACTORS,
    clean_environment,
    existing_actor_root,
    model_metadata,
    redact,
    smoke_overrides,
)


def test_smoke_prunes_unrelated_judges_and_bounds_training(tmp_path):
    base = OmegaConf.create(
        {
            "env": {
                "nemo_gym": {
                    "uv_venv_dir": "/fixture/gym_venvs",
                    "config_paths": ["fixture.yaml"],
                    "policy_model": {"responses_api_models": {"vllm_model": {"num_workers": 16}}},
                    "policy_model_reasoning_off": {
                        "_copy": "policy_model",
                        "responses_api_models": {"vllm_model": {"num_workers": 4}},
                    },
                    "unused_judge": {"resources_servers": {"general_qa": {"judge_server_url": "private-fixture"}}},
                }
            }
        }
    )
    result = smoke_overrides(
        base,
        tmp_path,
        tmp_path / "train.jsonl",
        {"tool_parser": "hermes", "eos": 7, "think_open": 8, "think_close": 9},
    )
    assert result["grpo"]["max_num_steps"] == 2
    assert result["grpo"]["num_prompts_per_step"] * result["grpo"]["num_generations_per_prompt"] == 4
    assert result["checkpointing"]["enabled"] is False
    assert result["env"]["_override_"] is True
    assert "unused_judge" not in result["env"]["nemo_gym"]
    assert result["env"]["nemo_gym"]["uv_venv_dir"] == "/fixture/gym_venvs"
    assert "unused_judge" in base.env.nemo_gym


def test_model_chat_template_selects_json_tool_parser_and_eos(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen3", "eos_token_id": 3}))
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps(
            {
                "chat_template": '<tool_call>{"name": name, "arguments": arguments}</tool_call>',
                "eos_token": "<|im_end|>",
                "added_tokens_decoder": {"5": {"content": "<|im_end|>"}},
            }
        )
    )
    result = model_metadata(tmp_path)
    assert result["tool_parser"] == "hermes" and result["eos"] == 5


def test_log_redaction_removes_credentials_from_json_and_tracebacks():
    secret = "fixture-private-key-123456789"
    text = '{"api_key": "' + secret + '"}\nerror: ' + secret
    assert secret not in redact(text, [secret])
    assert redact(text, [secret]).count("[REDACTED]") == 2


def test_long_run_paths_use_short_vllm_and_ray_ipc_directories(monkeypatch):
    # Exercise the actual Linux deployment paths without creating them on Mac.
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    project = Path("/workspace/storage-shared/nlp/tungks")
    root = project / "spreadsheet_all/runs/long-training-run"
    ipc = project / "a"
    env = clean_environment(root, ipc, project / "gym")
    assert env["TMPDIR"] == str(ipc / "tmp")
    assert env["RAY_TMPDIR"] == str(ipc)
    assert len(str(ipc / "tmp" / ("x" * 36)).encode()) <= 107
    assert len(str(ipc / "session_9999-99-99_99-99-99_999999_9999999/sockets/dash_MetricsHead").encode()) <= 107
    assert env["HF_HUB_OFFLINE"] == "1"


def test_six_gpu_profile_has_four_train_tp_ranks_and_two_rollout_tp_ranks(tmp_path):
    base = OmegaConf.create(
        {
            "env": {
                "nemo_gym": {
                    "policy_model": {"responses_api_models": {"vllm_model": {"num_workers": 16}}},
                }
            },
            "policy": {"model_name": "/models/Qwen3-32B", "max_total_sequence_length": 32768},
            "grpo": {"num_prompts_per_step": 256, "num_generations_per_prompt": 4},
        }
    )
    before = OmegaConf.to_container(base, resolve=False)
    overrides = smoke_overrides(
        base,
        tmp_path,
        tmp_path / "train.jsonl",
        {
            "tool_parser": "hermes",
            "eos": 151645,
            "think_open": 151667,
            "think_close": 151668,
        },
        six_gpu=True,
    )
    effective = OmegaConf.merge(base, overrides)
    assert effective.cluster.gpus_per_node == 6
    assert effective.policy.megatron_cfg.tensor_model_parallel_size == 4
    assert effective.policy.megatron_cfg.sequence_parallel is True
    assert effective.policy.megatron_cfg.activation_checkpointing is True
    assert effective.policy.precision == "bfloat16"
    assert effective.policy.megatron_cfg.optimizer.optimizer_cpu_offload is False
    assert effective.policy.generation.vllm_cfg.tensor_parallel_size == 2
    assert effective.policy.generation.colocated.resources.gpus_per_node == 2
    assert effective.policy.train_global_batch_size == 4
    assert effective.grpo.max_num_steps == 2
    assert effective.policy.max_total_sequence_length == 8192
    assert effective.policy.model_name == "/models/Qwen3-32B"
    assert OmegaConf.to_container(base, resolve=False) == before


def test_existing_actor_root_reuses_complete_default_and_respects_explicit_root(tmp_path, monkeypatch):
    import pytest

    monkeypatch.delenv("NEMO_RL_VENV_DIR", raising=False)
    root = tmp_path / "venvs"
    for actor in ACTORS:
        executable = root / actor / "bin/python"
        executable.parent.mkdir(parents=True)
        executable.touch()
    assert existing_actor_root(tmp_path) == root
    monkeypatch.setenv("NEMO_RL_VENV_DIR", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="refusing to bootstrap"):
        existing_actor_root(tmp_path)
