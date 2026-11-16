import json

from resources_servers.spreadsheet.packaging.analyze_training_smoke import analyze, prompt_key, trace_summary


def test_audit_counts_only_model_outputs_and_reports_missing_tool_results():
    result = {
        "reward": 0,
        "responses_create_params": {"input": [{"type": "function_call", "name": "fake", "call_id": "prompt"}]},
        "response": {
            "tools": [{"type": "function", "name": "fake"}],
            "output": [
                {"type": "function_call", "name": "read_sheet", "call_id": "model"},
                {"type": "function_call_output", "call_id": "model", "output": '{"cells": []}'},
                {"type": "function_call", "name": "write_cells", "call_id": "missing"},
            ],
        },
    }
    report = trace_summary(result)
    assert report["model_tool_calls"] == {"read_sheet": 1, "write_cells": 1}
    assert report["missing_tool_outputs"] == 1
    assert report["tool_errors"] == 0


def test_http_validation_errors_are_counted_without_dumping_inputs():
    payloads = [
        {"detail": [{"type": "missing", "loc": ["body", "sheet"], "input": "private-fixture"}]},
        {"error": "Formula validation failed [type=string_pattern_mismatch, input_value='private-fixture']"},
    ]
    for payload in payloads:
        result = {
            "reward": 0,
            "response": {"output": [{"type": "function_call_output", "call_id": "bad", "output": json.dumps(payload)}]},
        }
        report = trace_summary(result)
        assert report["tool_errors"] == 1
        assert "private-fixture" not in json.dumps(report)


def test_audit_rejects_reward_mismatch_instead_of_reporting_a_pass(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"exit_code": 0, "status": "exited"}))
    (tmp_path / "logs").mkdir()
    result = {"reward": 1, "response": {"output": []}}
    (tmp_path / "logs/train_data_step1.jsonl").write_text(json.dumps({"rewards": [0], "full_result": [result]}))
    report = analyze(tmp_path)
    assert any("differs" in problem for problem in report["problems"])
    assert report["reward_counts"] == {"1.0": 1}


def test_slim_trainer_results_match_agent_audit_without_counting_prefetch(tmp_path):
    prompt = {"responses_create_params": {"input": [{"role": "user", "content": "fixture"}]}}
    result = {
        **prompt,
        "reward": 1,
        "response": {
            "output": [
                {"type": "function_call", "name": "read_sheet", "call_id": "model"},
                {"type": "function_call_output", "call_id": "model", "output": "[]"},
            ]
        },
    }
    (tmp_path / "run.json").write_text(json.dumps({"exit_code": 0, "status": "exited"}))
    (tmp_path / "model_rollouts.jsonl").write_text((json.dumps(result) + "\n") * 3)
    (tmp_path / "logs").mkdir()
    for step in (1, 2):
        (tmp_path / f"logs/train_data_step{step}.jsonl").write_text(
            json.dumps({"rewards": [1], "full_result": [prompt], "advantages": [{"count": 7, "min": 0.0, "max": 0.0}]})
        )
    report = analyze(tmp_path)
    assert report["problems"] == []
    assert report["model_tool_calls"] == {"read_sheet": 2}
    assert report["unconsumed_agent_audits"] == 1
    assert report["advantages"]["all_zero_finite"] is True
    normalized = {
        "responses_create_params": {
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "fixture"}]}]
        }
    }
    assert prompt_key(prompt) == prompt_key(normalized)
