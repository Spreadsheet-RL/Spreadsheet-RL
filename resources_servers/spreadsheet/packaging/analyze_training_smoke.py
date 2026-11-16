"""Audit retained model trajectories and verifier-to-training reward propagation.

Only response.output entries count as model tool calls. Tool schemas, prompt
examples and trusted service-canary actions are excluded. No tool arguments or
workbook contents are printed. Logged steps alone do not prove optimizer updates.
"""

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def flatten(value):
    if isinstance(value, list):
        for item in value:
            yield from flatten(item)
    else:
        yield value


def prompt_key(result):
    messages = []
    for message in result["responses_create_params"]["input"]:
        content = message["content"]
        if isinstance(content, list) and all(
            isinstance(item, dict) and item.get("type") in {"input_text", "output_text"} for item in content
        ):
            content = "".join(item["text"] for item in content)
        messages.append({"role": message["role"], "content": content})
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def trace_summary(result):
    output = result["response"]["output"]
    calls = {item["call_id"]: item["name"] for item in output if item.get("type") == "function_call"}
    returned, errors, environment_errors = set(), 0, 0
    for item in output:
        if item.get("type") != "function_call_output":
            continue
        returned.add(item["call_id"])
        payload = item["output"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            errors += bool(payload.get("error") or payload.get("error_type") or payload.get("ok") is False)
            environment_errors += payload.get("error_type") == "environment_error"
    return {
        "task_id": result.get("task_id"),
        "reward": float(result["reward"]),
        "cell_accuracy": result.get("cell_accuracy"),
        "engine": result.get("engine"),
        "model_tool_calls": dict(Counter(calls.values())),
        "missing_tool_outputs": len(calls.keys() - returned),
        "tool_errors": errors,
        "environment_errors": environment_errors,
        "incomplete_details": result["response"].get("incomplete_details"),
    }


def analyze(root):
    receipt = json.loads((root / "run.json").read_text())
    audit_path = root / "model_rollouts.jsonl"
    audited = [json.loads(line) for line in audit_path.read_text().splitlines()] if audit_path.exists() else []
    remaining = list(audited)
    steps, traces, calls, problems, unrecognized = [], [], Counter(), [], []
    for path in sorted((root / "logs").rglob("train_data_step*.jsonl")):
        step = int(path.stem.removeprefix("train_data_step"))
        rows = 0
        for line in path.read_text().splitlines():
            row = json.loads(line)
            values = row.get("full_result", [])
            if isinstance(values, str):
                values = json.loads(values)
            results = list(flatten(values))
            rewards = list(flatten(row.get("rewards", [])))
            if len(results) != len(rewards) or not results:
                problems.append(f"step {step}: full_result/rewards length mismatch or missing traces")
                continue
            for result, reward in zip(results, rewards):
                if isinstance(result, str):
                    result = json.loads(result)
                if isinstance(result, dict) and "response" not in result and "responses_create_params" in result:
                    candidates = [
                        (index, record)
                        for index, record in enumerate(remaining)
                        if prompt_key(record) == prompt_key(result)
                        and math.isclose(float(reward), float(record["reward"]), rel_tol=1e-6, abs_tol=1e-6)
                    ]
                    if candidates:
                        index, result = candidates[0]
                        remaining.pop(index)
                if not isinstance(result, dict) or "response" not in result:
                    problems.append(f"step {step}: full response missing (possibly hash-only logs)")
                    if len(unrecognized) < 2:
                        unrecognized.append(
                            {key: type(value).__name__ for key, value in result.items()}
                            if isinstance(result, dict)
                            else {"type": type(result).__name__}
                        )
                    continue
                trace = trace_summary(result)
                trace["step"] = step
                traces.append(trace)
                calls.update(trace["model_tool_calls"])
                rows += 1
                if not math.isfinite(float(reward)) or not math.isclose(
                    float(reward), trace["reward"], rel_tol=1e-6, abs_tol=1e-6
                ):
                    problems.append(f"step {step}: verifier reward differs from logged training reward")
        steps.append({"step": step, "trajectory_count": rows, "path": str(path)})
    if {item["step"] for item in steps} != {1, 2}:
        problems.append("Expected retained trajectories for exactly steps 1 and 2")
    if not calls:
        problems.append("No model-generated tool calls were retained")
    if any(trace["missing_tool_outputs"] or trace["environment_errors"] for trace in traces):
        problems.append("At least one model call lacks an output or reports an environment error")
    return {
        "actor": "policy_model",
        "process_exit_code": receipt.get("exit_code"),
        "process_status": receipt.get("status"),
        "logged_steps": steps,
        "trajectory_count": len(traces),
        "agent_audit_count": len(audited),
        "unconsumed_agent_audits": len(remaining),
        "join": "Prompt-content and reward multiset when trainer retains only inputs",
        "model_tool_calls": dict(calls),
        "reward_counts": dict(Counter(str(trace["reward"]) for trace in traces)),
        "tool_error_count": sum(trace["tool_errors"] for trace in traces),
        "environment_error_count": sum(trace["environment_errors"] for trace in traces),
        "problems": problems,
        "unrecognized_result_shapes": unrecognized,
        "trajectories": traces,
        "scope": "Model tool/reward trace audit; inspect optimizer metrics separately",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve previous audit output; select a fresh output file")
    report = analyze(args.run_dir)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    with args.output.open("x") as stream:
        stream.write(text)
    print(text, end="", flush=True)
    raise SystemExit(bool(report["problems"]) or report["process_exit_code"] != 0)


if __name__ == "__main__":
    main()
