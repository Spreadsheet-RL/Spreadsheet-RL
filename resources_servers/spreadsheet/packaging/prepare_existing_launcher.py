"""Patch the existing full-training YAML/launcher after a verified smoke setup.

Uses the already-activated trainer environment. No installs, cache copies, data
conversion, GPU allocation or training launch. Both files receive private backups.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


if __package__:
    from .smoke_training import model_metadata
else:
    from smoke_training import model_metadata


def digest(body):
    return hashlib.sha256(body).hexdigest()


def patch_yaml(text, path, value):
    """Edit one explicit two-space mapping path, retaining unrelated comments."""
    from omegaconf import OmegaConf

    lines = text.splitlines(keepends=True)
    start, stop = 0, len(lines)
    for depth, key in enumerate(path):
        indent = depth * 2
        matches = [
            i for i in range(start, stop) if re.match(r"^" + " " * indent + re.escape(key) + r":(?:\s|$)", lines[i])
        ]
        if len(matches) > 1:
            raise ValueError("Duplicate mapping key: " + ".".join(path))
        if not matches:
            nested = value
            for child in reversed(path[depth:]):
                nested = {child: nested}
            fragment = OmegaConf.to_yaml(OmegaConf.create(nested), resolve=False)
            lines[stop:stop] = [" " * indent + line + "\n" for line in fragment.splitlines()]
            return "".join(lines)
        index = matches[0]
        end = next(
            (
                i
                for i in range(index + 1, stop)
                if lines[i].strip()
                and not lines[i].lstrip().startswith("#")
                and len(lines[i]) - len(lines[i].lstrip(" ")) <= indent
            ),
            stop,
        )
        while end > index + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        if depth == len(path) - 1:
            fragment = OmegaConf.to_yaml(OmegaConf.create({key: value}), resolve=False)
            lines[index:end] = [" " * indent + line + "\n" for line in fragment.splitlines()]
            return "".join(lines)
        if lines[index].split("#", 1)[0].strip() != key + ":":
            raise ValueError("Expected a block mapping at " + ".".join(path[: depth + 1]))
        start, stop = index + 1, end
    raise AssertionError("Empty YAML path")


def prepare_values(base, cache_root, config_paths, data_path, metadata):
    """Return only task wiring/correctness changes; preserve full-run sizing."""
    if len(base["data"]["train"]) != 1:
        raise ValueError("Review mixed training datasets before applying spreadsheet routing")
    env = copy.deepcopy(base["env"])
    gym = env["nemo_gym"]
    if "policy_model" not in gym:
        raise ValueError("Existing policy model server is required")
    for name, value in list(gym.items()):
        if isinstance(value, dict) and any(
            key in value for key in ("resources_servers", "responses_api_agents", "responses_api_models", "_copy")
        ):
            if name not in {"policy_model", "policy_model_reasoning_off"}:
                del gym[name]
    gym.update(uv_venv_dir=str(cache_root), config_paths=list(config_paths), skip_venv_if_present=True)
    env.update(should_use_nemo_gym=True)
    changes = [
        (["data", "train", "0", "data_path"], str(data_path)),
        (
            ["policy", "generation", "vllm_cfg", "http_server_serving_chat_kwargs", "tool_parser"],
            metadata["tool_parser"],
        ),
        (["token_ids", "eos"], metadata["eos"]),
    ]
    for key in ("think_open", "think_close"):
        if metadata.get(key) is not None:
            changes.append((["token_ids", key], metadata[key]))
    if int(base["policy"]["megatron_cfg"]["tensor_model_parallel_size"]) == 1:
        changes.append((["policy", "megatron_cfg", "sequence_parallel"], False))
    changes.append((["env"], env))
    return changes


def edit_launcher(text, config, driver, cache_root, direct_hosts):
    if "# Spreadsheet full-training IPC" in text:
        raise ValueError("Launcher already patched; inspect its receipt before applying again")
    if f'CONFIG_PATH="${{SCRIPT_DIR}}/{config.name}"' not in text:
        raise ValueError("Launcher must select this YAML in SCRIPT_DIR")
    old = 'python "${NEMO_RL_BASE_DIR}/examples/nemo_gym/run_grpo_nemo_gym.py" --config "${CONFIG_PATH}"'
    anchor = 'bash "${NEMO_RL_UTILS_DIR}/copy_gym_venvs.sh" "${CONFIG_PATH}"'
    pipeline = '} 2>&1 | tee "${RUN_LOG_DIR}/main.txt"'
    if text.count(old) != 1 or text.count(anchor) != 1 or text.count(pipeline) != 1:
        raise ValueError("Launcher differs from the supplied single-node script; no files changed")

    def quote(value):
        return "'" + str(value).replace("'", "'\"'\"'") + "'"

    block = (
        "# Spreadsheet full-training IPC (no smoke limits)\n"
        f"SPREADSHEET_DRIVER={quote(driver)}\n"
        "SPREADSHEET_IPC_ROOT=$(mktemp -d /tmp/spreadsheet-rl.XXXXXXXX) || exit 1\n"
        'export RAY_TMPDIR="${SPREADSHEET_IPC_ROOT}"\n'
        'export TMPDIR="${SPREADSHEET_IPC_ROOT}/tmp"\n'
        'mkdir -p "${TMPDIR}" || exit 1\n'
        f"export NEMO_GYM_VENV_DIR={quote(cache_root)}\n"
        "export UV_OFFLINE=1 PIP_NO_INDEX=1\n"
        'export NO_PROXY="${NO_PROXY:-},' + ",".join(direct_hosts) + '"\n'
        'export no_proxy="${NO_PROXY}"\n'
    )
    result = text.replace(anchor, block + anchor)
    result = result.replace(
        old,
        'python -B "${SPREADSHEET_DRIVER}" --entrypoint "${NEMO_RL_BASE_DIR}/examples/nemo_gym/run_grpo_nemo_gym.py" --config "${CONFIG_PATH}" --ray-root "${SPREADSHEET_IPC_ROOT}"',
    )
    result = result.replace(pipeline, pipeline + "\nspreadsheet_train_rc=${PIPESTATUS[0]}")
    return result.rstrip() + '\nexit "${spreadsheet_train_rc}"\n'


def write_private(path, body, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def replace_atomic(path, body):
    info = path.stat()
    mode = info.st_mode & 0o777
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".spreadsheet-replace-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "launcher", "smoke-config", "train-data", "private-config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    for path in (args.config, args.launcher, args.smoke_config, args.train_data, args.private_config):
        if path.is_symlink() or not path.is_file():
            parser.error("Inputs must be existing regular files")
    if args.config.resolve().parent != args.launcher.resolve().parent:
        parser.error("Use the original launcher and YAML from the same directory")
    if args.private_config.stat().st_mode & 0o077:
        parser.error("Private Gym config must be owner-only")
    from nemo_rl.utils.config import load_config, register_omegaconf_resolvers
    from omegaconf import OmegaConf
    from nemo_gym import PARENT_DIR

    if __package__:
        from .prepare_training_node import probe_venv, service_probe
        from .convert_training_data import DEFAULT_CONTRACT, verify_contract
    else:
        from prepare_training_node import probe_venv, service_probe
        from convert_training_data import DEFAULT_CONTRACT, verify_contract
    register_omegaconf_resolvers()
    contract = verify_contract(Path(load_config.__code__.co_filename).resolve().parents[2], DEFAULT_CONTRACT)
    base = load_config(args.config)
    reference = load_config(args.smoke_config)
    cache = Path(reference.env.nemo_gym.uv_venv_dir).resolve()
    gym_root = Path(PARENT_DIR).resolve()
    metadata = model_metadata(Path(base.policy.model_name))
    if metadata["eos"] is None:
        raise ValueError("Tokenizer EOS could not be verified")
    with args.train_data.open() as stream:
        count = 0
        for line in stream:
            row = json.loads(line)
            if row.get("agent_ref", {}).get("name") != "spreadsheet_agent":
                raise ValueError("Training data must route to spreadsheet_agent")
            count += 1
    if not count:
        raise ValueError("Empty training dataset")
    common = gym_root / "resources_servers/spreadsheet/configs/spreadsheet.yaml"
    paths = [
        "responses_api_models/vllm_model/configs/vllm_model_for_training.yaml",
        str(common),
        str(args.private_config.resolve()),
    ]
    if not common.is_file():
        raise ValueError("Spreadsheet Gym frontend is not installed")
    probes = [
        probe_venv(cache / path / ".venv", gym_root, module)
        for path, module in (
            ("responses_api_models/vllm_model", None),
            ("resources_servers/spreadsheet", "resources_servers.spreadsheet.app"),
            ("responses_api_agents/spreadsheet_agent", "responses_api_agents.spreadsheet_agent.app"),
        )
    ]
    if any(probe["versions"] != probes[0]["versions"] for probe in probes[1:]):
        raise ValueError("Existing Gym caches disagree on Ray/OpenAI versions")
    if any(Path(probe["prefix"]).resolve() != Path(probe["venv_path"]).resolve() for probe in probes):
        raise ValueError("A cached interpreter activates a different environment")
    service = service_probe(args.private_config, "LibreOffice 24.2.7.2 420(Build:2)")
    from urllib.parse import urlsplit

    hosts = [
        "127.0.0.1",
        "localhost",
        "0.0.0.0",
        socket.gethostname(),
        socket.gethostbyname(socket.gethostname()),
        urlsplit(service["service_url"]).hostname,
    ]
    original = {path: path.read_bytes() for path in (args.config, args.launcher)}
    changes = prepare_values(
        OmegaConf.to_container(base, resolve=False), cache, paths, args.train_data.resolve(), metadata
    )
    text = original[args.config].decode()
    expected = copy.deepcopy(base)
    for path, value in changes:
        if OmegaConf.select(expected, ".".join(path)) == value:
            continue
        OmegaConf.update(expected, ".".join(path), value, merge=False)
        if path[:2] == ["data", "train"]:
            text = patch_yaml(text, ["data", "train"], OmegaConf.to_container(expected.data.train, resolve=False))
        else:
            text = patch_yaml(text, path, {"_override_": True, **value} if path == ["env"] else value)
    driver = Path(__file__).with_name("smoke_driver.py").resolve()
    launcher = edit_launcher(original[args.launcher].decode(), args.config, driver, cache, hosts)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=".spreadsheet-full-", dir=args.config.parent
    ) as candidate:
        candidate.write(text)
        candidate.flush()
        if OmegaConf.to_container(load_config(Path(candidate.name)), resolve=False) != OmegaConf.to_container(
            expected, resolve=False
        ):
            raise ValueError("Effective settings differ beyond the intended migration")
    subprocess.run(["bash", "-n"], input=launcher, text=True, check=True)
    revision = uuid4().hex[:12]
    outputs = {args.config: text.encode(), args.launcher: launcher.encode()}
    receipt = {
        "status": "preparing",
        "training_started": False,
        "model": metadata,
        "rows": count,
        "cache_root": str(cache),
        "service": service,
        "trainer_contract": contract,
        "script_sha256": digest(Path(__file__).read_bytes()),
        "driver_sha256": digest(driver.read_bytes()),
        "files": [],
    }
    for path, before in original.items():
        if path.read_bytes() != before:
            raise ValueError("Input changed during preflight")
        backup = path.with_name(path.name + ".before-spreadsheet-full-" + revision + ".bak")
        write_private(backup, before)
        receipt["files"].append(
            {
                "path": str(path.resolve()),
                "backup": str(backup.resolve()),
                "before_sha256": digest(before),
                "after_sha256": digest(outputs[path]),
            }
        )
    receipt_path = args.config.with_name("spreadsheet-full-" + revision + ".json")
    write_private(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode())
    try:
        for path, body in outputs.items():
            replace_atomic(path, body)
    except Exception as error:
        for path, body in original.items():
            replace_atomic(path, body)
        receipt.update(status="rolled_back", error_type=type(error).__name__)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    receipt["status"] = "prepared"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "EXISTING_SPREADSHEET_LAUNCHER_PREPARED",
                "receipt": str(receipt_path),
                "rows": count,
                "training_started": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
