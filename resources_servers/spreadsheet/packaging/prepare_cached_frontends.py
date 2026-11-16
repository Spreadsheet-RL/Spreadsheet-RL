"""Copy existing Gym caches for the two spreadsheet frontends; install no packages."""

import argparse
import json
import os
import re
import shlex
import shutil
import socket
from pathlib import Path


CACHES = (
    ("resources_servers/math_with_judge", "resources_servers/spreadsheet"),
    ("responses_api_agents/simple_agent", "responses_api_agents/spreadsheet_agent"),
)


def complete(path):
    return all((path / "bin" / name).is_file() for name in ("python", "activate"))


def activation_text(path):
    activation = path / "bin/activate"
    if activation.is_symlink() or activation.parent.is_symlink():
        raise ValueError(f"Bash activation must be a regular file: {activation}")
    text = activation.read_text()
    if len(re.findall(r"^VIRTUAL_ENV=.*$", text, re.MULTILINE)) != 1:
        raise ValueError(f"Unsupported Bash activation format: {activation}")
    return text


def prepare(root, source_root=None, *, include_model=False, relocate=False):
    root = root.resolve()
    source_root = Path(source_root).resolve() if source_root else root
    mappings = (
        CACHES + (("responses_api_models/vllm_model", "responses_api_models/vllm_model"),) if include_model else CACHES
    )
    plans = []
    # Validate both destinations before creating either one. Preserve existing
    # caches and incomplete evidence rather than merging or overwriting them.
    for base, module in mappings:
        source, target = source_root / base / ".venv", root / module / ".venv"
        exists = target.exists() or target.is_symlink()
        if exists and (target.is_symlink() or not complete(target)):
            raise ValueError(f"Existing frontend cache is linked or incomplete; inspect: {target}")
        if not exists and not complete(source):
            raise ValueError(f"Existing base cache is missing or incomplete: {source}")
        if relocate:
            text = activation_text(target if exists else source)
            expected = "VIRTUAL_ENV=" + shlex.quote(str(target))
            if exists and expected not in text.splitlines():
                raise ValueError(f"Existing cache activation points elsewhere; inspect: {target}")
        plans.append((source, target, exists))
    result = []
    for source, target, exists in plans:
        if not exists:
            print(
                json.dumps({"event": "COPY_EXISTING_GYM_CACHE", "source": str(source), "target": str(target)}),
                flush=True,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            # Dereference a linked source root, while preserving normal venv
            # interpreter/library symlinks inside the copied directory.
            shutil.copytree(source.resolve(), target, symlinks=True)
            if relocate:
                text = activation_text(target)
                updated = re.sub(
                    r"^VIRTUAL_ENV=.*$", lambda _: "VIRTUAL_ENV=" + shlex.quote(str(target)), text, flags=re.MULTILINE
                )
                (target / "bin/activate").write_text(updated)
            if not complete(target):
                raise ValueError(f"Copied frontend cache is incomplete: {target}")
        result.append({"source": str(source), "target": str(target), "status": "reused" if exists else "copied"})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--include-model", action="store_true")
    parser.add_argument("--relocate-activation", action="store_true")
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong node; run this only on the selected training instance")
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        receipt = {"status": "applying", "hostname": socket.gethostname(), "venv_root": str(args.venv_root.resolve())}
        json.dump(receipt, stream)
        stream.flush()
        try:
            receipt.update(
                caches=prepare(
                    args.venv_root,
                    args.source_root,
                    include_model=args.include_model,
                    relocate=args.relocate_activation,
                ),
                status="prepared",
            )
        except Exception as exc:
            receipt.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            stream.seek(0)
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
            stream.truncate()
    print(
        json.dumps(
            {"event": "SPREADSHEET_CACHES_PREPARED", "receipt": str(args.receipt), "caches": receipt["caches"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
