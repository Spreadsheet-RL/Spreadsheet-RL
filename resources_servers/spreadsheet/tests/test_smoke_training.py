import json
from pathlib import Path

from omegaconf import OmegaConf

from resources_servers.spreadsheet.packaging.smoke_training import (
    clean_environment,
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
    env = clean_environment(root, ipc / "ray", project / "gym")
    assert env["TMPDIR"] == str(ipc / "tmp")
    assert env["RAY_TMPDIR"] == str(ipc)
    assert len(str(ipc / "tmp" / ("x" * 36)).encode()) <= 107
    assert env["HF_HUB_OFFLINE"] == "1"
