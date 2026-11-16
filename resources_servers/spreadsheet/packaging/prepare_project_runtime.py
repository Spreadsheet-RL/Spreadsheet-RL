"""Prepare an allowed project-local Gym cache and an inherited trainer YAML.

Existing trainer/Gym source, /opt caches, qualified data and private keys are read
only. All persistent writes are confined to the new --runtime-root directory.
"""

import argparse
import json
import os
import socket
from pathlib import Path


if __package__:
    from .prepare_training_node import sibling
else:
    from prepare_training_node import sibling


def write_config(path, base, cache_root):
    text = (
        "defaults: " + json.dumps(str(base.resolve())) + "\n"
        "env:\n  nemo_gym:\n    uv_venv_dir: " + json.dumps(str(cache_root.resolve())) + "\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("trainer-root", "base-config", "runtime-root", "qualified-dir", "output-dir", "private-config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-cache-root", type=Path, default=Path("/opt/gym_venvs"))
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    root = args.runtime_root.resolve()
    if root.exists():
        parser.error("Select a fresh runtime-root; existing runtime trees are preserved")
    if args.private_config.is_symlink() or args.private_config.stat().st_mode & 0o077:
        parser.error("A regular owner-only private config is required")
    if not args.output_dir.is_dir():
        parser.error("Converted data must already exist; this entrypoint never creates or modifies it")

    data, integration, setup, cache = (
        sibling(name)
        for name in (
            "convert_training_data",
            "install_gym_integration",
            "prepare_training_node",
            "prepare_cached_frontends",
        )
    )
    contract = data.verify_contract(args.trainer_root.resolve(), data.DEFAULT_CONTRACT)
    from nemo_rl.utils.config import load_config
    from omegaconf import OmegaConf

    import nemo_gym

    if Path(load_config.__code__.co_filename).resolve() != args.trainer_root.resolve() / "nemo_rl/utils/config.py":
        raise ValueError("The active config loader is from a different trainer checkout")
    gym_root = Path(nemo_gym.PARENT_DIR).resolve()
    base_sha = data.digest(args.base_config)
    base_config = load_config(args.base_config)
    if OmegaConf.select(base_config, "env.nemo_gym.skip_venv_if_present") is not True:
        raise ValueError("Existing skip_venv_if_present=true is required")
    common = str(gym_root / "resources_servers/spreadsheet/configs/spreadsheet.yaml")
    private = str(args.private_config.resolve())
    config_paths = list(OmegaConf.select(base_config, "env.nemo_gym.config_paths") or [])
    expected_paths = ["responses_api_models/vllm_model/configs/vllm_model_for_training.yaml", common, private]
    if config_paths != expected_paths:
        raise ValueError("Review additional/reordered Gym servers before preparing this three-server runtime")
    installed = {}
    for relative in integration.FILES:
        sha = data.digest(gym_root / relative)
        if sha != data.digest(args.source_root / relative):
            raise ValueError(f"Installed frontend source differs; no protected source was modified: {relative}")
        installed[relative] = sha
    conversion = data.convert(args.qualified_dir.resolve(), args.output_dir.resolve(), "spreadsheet_agent")
    train = OmegaConf.select(base_config, "data.train")
    if len(train) != 1 or train[0].data_path != str(args.output_dir.resolve() / "train.jsonl"):
        raise ValueError("The existing trainer config does not select the verified spreadsheet data")
    compatibility = json.loads((args.qualified_dir / "compatibility.json").read_text())
    service = setup.service_probe(args.private_config, compatibility["engine"])

    root.mkdir(mode=0o700)
    cache_root, config_path = root / "gym_venvs", root / "spreadsheet.yaml"
    receipt = {
        "status": "applying",
        "phase": "copy_project_caches",
        "hostname": socket.gethostname(),
        "trainer_root": str(args.trainer_root),
        "trainer_contract": contract,
        "base_config": str(args.base_config),
        "base_config_sha256": base_sha,
        "gym_root": str(gym_root),
        "installed_frontends": installed,
        "data": conversion,
        "service": service,
        "training_started": False,
    }
    release = args.source_root / "RELEASE.json"
    if release.is_file():
        receipt["source_commit"] = json.loads(release.read_text())["source_commit"]
    setup.save_receipt(root, receipt)
    try:
        receipt["caches"] = cache.prepare(cache_root, args.source_cache_root, include_model=True, relocate=True)
        receipt.update(phase="verify_project_config")
        setup.save_receipt(root, receipt)
        write_config(config_path, args.base_config, cache_root)
        effective = OmegaConf.to_container(load_config(config_path), resolve=False)
        expected = OmegaConf.to_container(base_config, resolve=False)
        expected["env"]["nemo_gym"]["uv_venv_dir"] = str(cache_root)
        if effective != expected or data.digest(args.base_config) != base_sha:
            raise ValueError("Effective settings changed beyond the Gym cache path")
        receipt.update(phase="check_frontend_imports", trainer_config=str(config_path))
        setup.save_receipt(root, receipt)
        probes = [
            setup.probe_venv(cache_root / relative / ".venv", gym_root, module)
            for relative, module in (
                ("responses_api_models/vllm_model", None),
                ("resources_servers/spreadsheet", "resources_servers.spreadsheet.app"),
                ("responses_api_agents/spreadsheet_agent", "responses_api_agents.spreadsheet_agent.app"),
            )
        ]
        for probe in probes:
            if Path(probe["prefix"]).resolve() != Path(probe["venv_path"]).resolve():
                raise ValueError("Copied cache activates a different Python environment")
        if any(probe["versions"] != probes[0]["versions"] for probe in probes[1:]):
            raise ValueError("Project caches disagree on Ray/OpenAI versions")
        receipt.update(
            status="prepared", phase="complete", venv_probes=probes, trainer_config_sha256=data.digest(config_path)
        )
    except Exception as exc:
        receipt.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        setup.save_receipt(root, receipt)
    setup.emit(
        "TRAINING_NODE_PREPARED",
        receipt=str(root / "setup.json"),
        trainer_config=str(config_path),
        venv_root=str(cache_root),
        rows=conversion["rows"],
        training_started=False,
    )


if __name__ == "__main__":
    main()
