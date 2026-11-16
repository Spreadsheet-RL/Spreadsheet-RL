"""Install the two spreadsheet frontend modules and register their Gym configs.

Requires an explicitly selected active Gym checkout and trainer-readable private
override. Does not install packages, copy backend/oracle data, or launch training.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


FILES = (
    "resources_servers/spreadsheet/app.py",
    "resources_servers/spreadsheet/schemas.py",
    "resources_servers/spreadsheet/requirements.txt",
    "resources_servers/spreadsheet/configs/spreadsheet.yaml",
    "responses_api_agents/spreadsheet_agent/app.py",
    "responses_api_agents/spreadsheet_agent/requirements.txt",
)


def data_tools():
    path = Path(__file__).with_name("convert_training_data.py")
    spec = importlib.util.spec_from_file_location("spreadsheet_data_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register_configs(config, additions):
    """Append only the required paths under env.nemo_gym.config_paths."""
    if config.is_symlink():
        raise ValueError("Trainer config is a symlink; choose its intended target explicitly")
    original = config.read_bytes()
    lines = original.decode().splitlines(keepends=True)

    def section(start, stop, expression):
        matches = [i for i in range(start, stop) if re.fullmatch(expression + r"[ \t]*(?:#.*)?\r?\n?", lines[i])]
        if len(matches) != 1:
            raise ValueError("Expected explicit env.nemo_gym.config_paths in the trainer YAML")
        index = matches[0]
        indent = len(lines[index]) - len(lines[index].lstrip(" "))
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
        return index, end

    env, env_end = section(0, len(lines), "env:")
    gym, gym_end = section(env + 1, env_end, "  nemo_gym:")
    paths, end = section(gym + 1, gym_end, "    config_paths:")
    existing, last_entry = [], paths
    for line in lines[paths + 1 : end]:
        match = re.fullmatch(r"      - (.*?)(?:\r?\n)?", line)
        if match:
            value = re.split(r"[ \t]+(?=#)", match[1], maxsplit=1)[0].strip()
            existing.append(json.loads(value) if value.startswith('"') else value.strip("'"))
        elif line.strip() and not line.lstrip().startswith("#"):
            raise ValueError("Unsupported config_paths layout; refusing to rewrite it")
    missing = [str(path) for path in additions if str(path) not in existing]
    if not missing:
        if [existing.index(str(path)) for path in additions] != sorted(
            existing.index(str(path)) for path in additions
        ):
            raise ValueError("Existing spreadsheet config must precede its private override")
        return None
    if str(additions[-1]) in existing and str(additions[0]) not in existing:
        raise ValueError("Private override is already loaded before the missing spreadsheet config; inspect ordering")
    newline = "\r\n" if "\r\n" in lines[paths] else "\n"
    for i in range(paths + 1, end):
        if re.match(r"      - ", lines[i]):
            last_entry = i
    if not lines[last_entry].endswith("\n"):
        lines[last_entry] += newline
    lines[last_entry + 1 : last_entry + 1] = ["      - " + json.dumps(path) + newline for path in missing]
    return data_tools().replace_yaml(config, original, "".join(lines).encode())


def install(source, target):
    tools = data_tools()
    for name in ("nemo_gym/base_resources_server.py", "responses_api_agents/simple_agent/app.py"):
        if not (target / name).is_file():
            raise ValueError("gym-root is not the explicitly selected Gym source checkout")
    hashes = {relative: tools.digest(source / relative) for relative in FILES}
    # Inspect every destination before installing either module. Never replace
    # conflicting source code or link through an existing module directory.
    for relative in FILES:
        destination = target / relative
        if any(parent.is_symlink() for parent in destination.parents if parent != target.parent):
            raise ValueError("Gym destination traverses a symlink; choose the real checkout path")
        if destination.exists() and (not destination.is_file() or tools.digest(destination) != hashes[relative]):
            raise ValueError(f"Existing Gym file differs: {relative}; no files were replaced")
    modules = ("resources_servers/spreadsheet", "responses_api_agents/spreadsheet_agent")
    for module in modules:
        destination = target / module
        if destination.exists() and not all(
            (target / name).is_file() for name in FILES if name.startswith(module + "/")
        ):
            raise ValueError("An incomplete module directory exists; inspect it before installing")
    for module in modules:
        destination = target / module
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".spreadsheet-overlay-", dir=destination.parent))
        for relative in FILES:
            if relative.startswith(module + "/"):
                output = stage / Path(relative).relative_to(module)
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, output)
                if tools.digest(output) != hashes[relative]:
                    raise ValueError("Integration source changed during copy")
        stage.rename(destination)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--gym-root", type=Path, required=True)
    parser.add_argument("--trainer-config", type=Path, required=True)
    parser.add_argument(
        "--private-config", type=Path, required=True, help="Private Gym YAML readable at this path by the trainer"
    )
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists() or args.receipt.is_symlink():
        parser.error("Choose a new receipt path; an existing receipt is never replaced")
    if args.trainer_config.is_symlink():
        parser.error("trainer-config is a symlink; choose its intended target explicitly")
    if not args.private_config.is_file() or args.private_config.stat().st_mode & 0o077:
        parser.error("private-config must exist with owner-only file permissions")
    target = args.gym_root.resolve()
    tools = data_tools()
    # Validate the YAML layout before changing source. The full edit follows
    # source installation; the same minimal path append is safe to retry.
    with tempfile.TemporaryDirectory() as temporary:
        preview = Path(temporary) / "trainer.yaml"
        preview.write_bytes(args.trainer_config.read_bytes())
        register_configs(
            preview, [target / "resources_servers/spreadsheet/configs/spreadsheet.yaml", args.private_config.resolve()]
        )
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = install(args.source_root.resolve(), target)
    backup = register_configs(
        args.trainer_config,
        [target / "resources_servers/spreadsheet/configs/spreadsheet.yaml", args.private_config.resolve()],
    )
    after = tools.digest(args.trainer_config)
    receipt = {
        "schema_version": 1,
        "script_sha256": tools.digest(Path(__file__)),
        "source_root": str(args.source_root.resolve()),
        "gym_root": str(target),
        "files": files,
        "trainer_config": str(args.trainer_config.resolve()),
        "trainer_before_sha256": tools.digest(Path(backup)) if backup else after,
        "trainer_after_sha256": after,
        "backup": backup,
        "private_config_path": str(args.private_config.resolve()),
        "runtime_import_check": "not_run",
        "training": "not_started",
    }
    descriptor = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "event": "GYM_SPREADSHEET_INSTALLED",
                "gym_root": str(target),
                "files": len(files),
                "receipt": str(args.receipt),
            }
        )
    )


if __name__ == "__main__":
    main()
