"""Validate the three existing Spreadsheet Gym venvs; never copy/install venvs."""

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import sys


def validate(config, gym_root, probe):
    cache = Path(config.env.nemo_gym.uv_venv_dir).resolve()
    if not config.env.nemo_gym.skip_venv_if_present:
        raise ValueError("Require skip_venv_if_present for the existing-cache launcher")
    versions = {name: metadata.version(name) for name in ("ray", "openai")}
    for relative, module in (
        ("responses_api_models/vllm_model", None),
        ("resources_servers/spreadsheet", "resources_servers.spreadsheet.app"),
        ("responses_api_agents/spreadsheet_agent", "responses_api_agents.spreadsheet_agent.app"),
    ):
        result = probe(cache / relative / ".venv", gym_root, module)
        if Path(result["prefix"]).resolve() != Path(result["venv_path"]).resolve():
            raise ValueError("Cached activation selects a different venv")
        if result["versions"] != versions:
            raise ValueError("Trainer and cached Ray/OpenAI versions differ")
        print(json.dumps({"event": "SPREADSHEET_CACHE_VALIDATED", **result}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--helpers-root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    sys.path.insert(0, str(args.helpers_root))
    from prepare_training_node import probe_venv
    from nemo_rl.utils.config import load_config, register_omegaconf_resolvers
    from nemo_gym import PARENT_DIR

    register_omegaconf_resolvers()
    validate(load_config(args.config), Path(PARENT_DIR).resolve(), probe_venv)


if __name__ == "__main__":
    main()
