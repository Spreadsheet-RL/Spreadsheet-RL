"""Inspect a moved trainer/cache without sourcing scripts or changing files.

Uses only Python's standard library. Child Python probes import only standard
library modules, disable bytecode writes, and never initialize Ray or CUDA.
"""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess


LEGACY_ROOTS = ("/workspace/storage-shared/nlp/vai-1.4-30b", "/workspace/storage-shared/nlp/tungks")
PATH_PATTERN = re.compile(r"/(?:workspace|opt|usr|tmp|var)/[^\s\"'{}\[\](),;\\$]+")
SELECTED_KEYS = {
    "defaults",
    "model_name",
    "train_global_batch_size",
    "train_micro_batch_size",
    "num_prompts_per_step",
    "num_generations_per_prompt",
    "tensor_model_parallel_size",
    "pipeline_model_parallel_size",
    "context_parallel_size",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "expert_parallel_size",
    "sequence_parallel",
    "uv_venv_dir",
    "gpus_per_node",
    "num_nodes",
}


def file_report(path, selected_keys=False):
    result = {"path": str(path), "exists": path.is_file()}
    if not result["exists"]:
        return result
    text = path.read_text()
    result["legacy_reference_lines"] = [
        index for index, line in enumerate(text.splitlines(), 1) if any(root in line for root in LEGACY_ROOTS)
    ]
    result["absolute_paths"] = [
        {"path": value, "exists": Path(value).exists()}
        for value in sorted(set(PATH_PATTERN.findall("\n".join(line.split("#", 1)[0] for line in text.splitlines()))))
    ]
    if selected_keys:
        result["explicit_yaml_fields_only"] = [
            {"line": index, "field": match.group(1), "value": match.group(2).split("#", 1)[0].strip()}
            for index, line in enumerate(text.splitlines(), 1)
            if (match := re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", line)) and match.group(1) in SELECTED_KEYS
        ]
    return result


def venv_report(venv):
    python = venv / "bin/python"
    result = {"venv": str(venv), "python_exists": python.is_file(), "python_executable": os.access(python, os.X_OK)}
    activate = venv / "bin/activate"
    result["activation_virtual_env"] = None
    if activate.is_file():
        # Ignore the Cygwin $(cygpath ...) branch; never evaluate shell text.
        match = re.search(
            r"^\s*(?:export\s+)?VIRTUAL_ENV=(?:\"(/[^\"\n]+)\"|'(/[^'\n]+)'|(/[^\s;]+))\s*$",
            activate.read_text(),
            re.MULTILINE,
        )
        if match:
            value = next(group for group in match.groups() if group is not None)
            result["activation_virtual_env"] = value
            result["activation_matches_location"] = Path(value).resolve() == venv.resolve()
    if not result["python_exists"] or not result["python_executable"]:
        return result
    code = """
import importlib.metadata as metadata
import json
import sys
versions = {}
for name in ('ray', 'openai', 'omegaconf'):
    try:
        versions[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        versions[name] = None
print('RUNTIME_PATH_PROBE=' + json.dumps(dict(
    python=sys.executable, prefix=sys.prefix, version=sys.version.split()[0], versions=versions,
)))
"""
    try:
        process = subprocess.run(
            [str(python), "-I", "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"},
        )
        result["returncode"] = process.returncode
        marker = "RUNTIME_PATH_PROBE="
        reports = [json.loads(line[len(marker) :]) for line in process.stdout.splitlines() if line.startswith(marker)]
        if reports:
            result["python_probe"] = reports[-1]
            result["python_prefix_matches_location"] = Path(reports[-1]["prefix"]).resolve() == venv.resolve()
        else:
            result["error"] = "python_probe_did_not_return_metadata"
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = type(error).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("trainer-root", "config", "smoke-config", "cache-root"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    utilities = args.trainer_root / "custom_utils/utils_training"
    gym = args.trainer_root / "3rdparty/Gym-workspace/Gym"
    report = {
        "event": "RELOCATED_RUNTIME_INSPECTION",
        "hostname": socket.gethostname(),
        "scripts_sourced": False,
        "training_started": False,
        "trainer_root": str(args.trainer_root),
        "activation": file_report(utilities / "use_custom_opt.sh"),
        "cache_helper": file_report(utilities / "copy_gym_venvs.sh"),
        "config": file_report(args.config, selected_keys=True),
        "historical_smoke": file_report(args.smoke_config),
        "required_files": [
            {"path": str(path), "exists": path.is_file()}
            for path in (
                args.trainer_root / "nemo_rl/__init__.py",
                gym / "nemo_gym/__init__.py",
                gym / "resources_servers/spreadsheet/app.py",
                gym / "responses_api_agents/spreadsheet_agent/app.py",
            )
        ],
        "nltk_data": {
            "path": str(args.trainer_root.parent / "nltk_data"),
            "exists": (args.trainer_root.parent / "nltk_data").is_dir(),
        },
        "cached_venvs": [
            venv_report(args.cache_root / relative / ".venv")
            for relative in (
                "responses_api_models/vllm_model",
                "resources_servers/spreadsheet",
                "responses_api_agents/spreadsheet_agent",
            )
        ],
    }
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
