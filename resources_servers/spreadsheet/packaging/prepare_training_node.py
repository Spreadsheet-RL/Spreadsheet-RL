"""Apply the source/data scripts using the existing trainer activation/cache helper.

No custom venv aliases, package installation, Ray initialization or training.
"""

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def sibling(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("spreadsheet_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def save_receipt(run, value):
    temporary = run / ".setup.json.partial"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(run / "setup.json")


def run_cache_helper(cache_helper, trainer_config, cache_log):
    env = dict(os.environ, UV_OFFLINE="1", PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    # pathlib.Path.open does not accept opener; the builtin open does.
    with open(cache_log, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as log:
        return subprocess.run(
            ["bash", str(cache_helper), str(trainer_config)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )


def probe_venv(venv, gym_root, module_name=None):
    if not (venv / "bin/python").is_file() or not (venv / "bin/activate").is_file():
        raise ValueError(f"Existing Gym sub-venv is missing: {venv}")
    code = """
import importlib, importlib.metadata as metadata, json, sys
from pathlib import Path
root, name = Path(sys.argv[1]).resolve(), sys.argv[2]
sys.path.insert(0, str(root))
try:
    import nemo_gym
    if Path(nemo_gym.PARENT_DIR).resolve() != root:
        raise RuntimeError("wrong_gym_source")
    versions = {key: metadata.version(key) for key in ("ray", "openai")}
    location = None
    if name:
        mod = importlib.import_module(name)
        location = str(Path(mod.__file__).resolve())
        if Path(location) != root.joinpath(*name.split(".")).with_suffix(".py"):
            raise RuntimeError("wrong_frontend_source")
    result = {"ok": True, "python": sys.executable, "prefix": sys.prefix, "versions": versions, "module_file": location}
except Exception as exc:
    result = {"ok": False, "error_type": type(exc).__name__, "missing_module": getattr(exc, "name", None)}
print("SPREADSHEET_IMPORT_PROBE=" + json.dumps(result))
"""
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'source "$1/bin/activate" >/dev/null 2>&1 || exit 125; exec python -I -B -c "$2" "$3" "$4"',
            "spreadsheet-import-check",
            str(venv),
            code,
            str(gym_root),
            module_name or "",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=120,
    )
    marker = "SPREADSHEET_IMPORT_PROBE="
    reports = [json.loads(line.split(marker, 1)[1]) for line in result.stdout.splitlines() if marker in line]
    if result.returncode or not reports or not reports[-1].get("ok"):
        error = reports[-1].get("error_type") if reports else "activation_or_import_failed"
        raise ValueError(f"Existing venv import check failed: {venv}; {error}")
    return {"venv_path": str(venv), **reports[-1]}


def service_probe(private, expected_engine):
    from omegaconf import OmegaConf

    try:
        settings = OmegaConf.load(private).spreadsheet_resources.resources_servers.spreadsheet
        base, key = settings.service_url.rstrip("/"), str(settings.api_key)
        if len(key) < 32 or len(str(settings.session_secret_key)) < 32:
            raise ValueError
        url = urlsplit(base)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError
    except Exception:
        raise ValueError("Private Gym override contains an invalid service URL or keys") from None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, auth=False):
        request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key} if auth else {})
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    health = get("/health")
    if (
        health.get("status") != "ready"
        or health.get("engine") != expected_engine
        or health.get("python_isolation") != "chroot_uid_seccomp"
    ):
        raise ValueError("Service health does not match the qualified backend")
    tools = get("/tools", True)["tools"]
    expected = {
        "list_sheets",
        "inspect_range",
        "find_cells",
        "write_range",
        "fill_formula",
        "format_range",
        "manage_sheet",
        "clear_range",
        "delete_rows",
        "delete_columns",
        "recalculate_and_read",
        "code_interpreter",
    }
    if len(tools) != len(expected) or set(tools) != expected:
        raise ValueError("Spreadsheet service tool set is incomplete")
    return {"service_url": base, "health": health, "tool_count": len(tools)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("trainer-root", "trainer-config", "qualified-dir", "output-dir", "private-config", "run-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--activation-script", type=Path)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong node; run this setup on the explicitly selected training instance")
    if (
        args.private_config.is_symlink()
        or not args.private_config.is_file()
        or args.private_config.stat().st_mode & 0o077
    ):
        parser.error("A trainer-readable shared private config with mode 0600 is required")
    data, overlay = sibling("convert_training_data"), sibling("install_gym_integration")
    contract = data.verify_contract(args.trainer_root.resolve(), data.DEFAULT_CONTRACT)

    # Use the same interpreter/PYTHONPATH selected by use_custom_opt.sh.
    from nemo_rl.utils.config import load_config
    from omegaconf import OmegaConf

    import nemo_gym

    spec = importlib.util.find_spec("nemo_rl")
    locations = (
        [Path(spec.origin).resolve().parent]
        if spec and spec.origin
        else [Path(path).resolve() for path in (spec.submodule_search_locations or [])]
        if spec
        else []
    )
    if args.trainer_root.resolve() / "nemo_rl" not in locations:
        raise ValueError("The activated interpreter imports a different nemo_rl checkout")
    gym_root = Path(nemo_gym.PARENT_DIR).resolve()
    config = load_config(args.trainer_config)
    if OmegaConf.select(config, "env.nemo_gym.skip_venv_if_present") is not True:
        raise ValueError("Keep env.nemo_gym.skip_venv_if_present=true to reuse existing environments")
    root_value = OmegaConf.select(config, "env.nemo_gym.uv_venv_dir") or os.environ.get("NEMO_GYM_VENV_DIR")
    dotenv = args.trainer_root / "nemo_gym_env.yaml"
    if not root_value and dotenv.is_file():
        root_value = OmegaConf.select(OmegaConf.load(dotenv), "uv_venv_dir")
    venv_root = Path(root_value or getattr(nemo_gym, "WORKING_DIR", gym_root)).resolve()
    cache_helper = args.trainer_root / "custom_utils/utils_training/copy_gym_venvs.sh"
    if not cache_helper.is_file():
        raise ValueError("The existing copy_gym_venvs.sh helper is missing")
    compatibility = json.loads((args.qualified_dir / "compatibility.json").read_text())
    service = service_probe(args.private_config, compatibility["engine"])
    run = args.run_dir.resolve() / ("setup-" + uuid4().hex[:12])
    run.mkdir(parents=True, mode=0o700)
    emit("TRAINING_SETUP_CONTEXT", gym_root=str(gym_root), venv_root=str(venv_root), run_dir=str(run))
    receipt = {
        "schema_version": 1,
        "status": "applying",
        "phase": "install_source",
        "hostname": socket.gethostname(),
        "driver_python": sys.executable,
        "script_sha256": data.digest(Path(__file__)),
        "trainer_contract": contract,
        "gym_root": str(gym_root),
        "venv_root": str(venv_root),
        "service": service,
        "cache_helper_sha256": data.digest(cache_helper),
        "training_started": False,
    }
    if args.activation_script:
        receipt["activation_script_sha256"] = data.digest(args.activation_script)
    release = args.source_root / "RELEASE.json"
    if release.is_file():
        receipt["source_release"] = json.loads(release.read_text())["source_commit"]
    save_receipt(run, receipt)

    installed = overlay.install(args.source_root.resolve(), gym_root)
    config_backup = overlay.register_configs(
        args.trainer_config,
        [
            gym_root / "resources_servers/spreadsheet/configs/spreadsheet.yaml",
            args.private_config.resolve(),
        ],
    )
    receipt.update(files=installed, gym_config_backup=config_backup, phase="convert_data")
    save_receipt(run, receipt)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("convert_training_data.py")),
            "--trainer-root",
            str(args.trainer_root),
            "--trainer-config",
            str(args.trainer_config),
            "--input-dir",
            str(args.qualified_dir),
            "--output-dir",
            str(args.output_dir),
            "--receipt",
            str(run / "data-conversion.json"),
        ],
        check=True,
    )
    receipt.update(
        phase="reuse_existing_cache",
        data_receipt=str(run / "data-conversion.json"),
        trainer_config_sha256=data.digest(args.trainer_config),
    )
    save_receipt(run, receipt)

    # The project's helper only recognizes relative resource config paths and
    # does not copy custom-agent caches. Fill those two known gaps by the same
    # existing-cache copy recipe, then retain the launcher helper for the rest.
    receipt.update(phase="prepare_frontend_caches")
    save_receipt(run, receipt)
    receipt["frontend_caches"] = sibling("prepare_cached_frontends").prepare(venv_root)
    receipt.update(phase="reuse_existing_cache")
    save_receipt(run, receipt)
    emit("REUSING_EXISTING_CACHE_HELPER", path=str(cache_helper))
    cache_log = run / "cache-copy.private.log"
    receipt["cache_log"] = str(cache_log)
    save_receipt(run, receipt)
    completed = run_cache_helper(cache_helper, args.trainer_config, cache_log)
    if completed.returncode:
        receipt.update(status="failed", cache_helper_exit_code=completed.returncode)
        save_receipt(run, receipt)
        raise RuntimeError(
            f"Existing copy_gym_venvs.sh failed with exit {completed.returncode}; private log: {cache_log}"
        )
    receipt.update(phase="check_imports", cache_helper_exit_code=0)
    save_receipt(run, receipt)
    reference = probe_venv(venv_root / "responses_api_models/vllm_model/.venv", gym_root)
    probes = [
        probe_venv(venv_root / relative / ".venv", gym_root, name)
        for relative, name in (
            ("resources_servers/spreadsheet", "resources_servers.spreadsheet.app"),
            ("responses_api_agents/spreadsheet_agent", "responses_api_agents.spreadsheet_agent.app"),
        )
    ]
    if any(probe["versions"] != reference["versions"] for probe in probes):
        raise ValueError("Spreadsheet cached venvs disagree with the existing model server's Ray/OpenAI versions")
    receipt.update(status="prepared", phase="complete", venv_probes=probes, reference_venv=reference)
    path = run / "setup.json"
    save_receipt(run, receipt)
    emit(
        "TRAINING_NODE_PREPARED",
        receipt=str(path),
        data_receipt=str(run / "data-conversion.json"),
        gym_root=str(gym_root),
        venv_root=str(venv_root),
        training_started=False,
    )


if __name__ == "__main__":
    main()
