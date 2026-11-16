"""Copy existing Gym caches for the two spreadsheet frontends; install no packages."""

import argparse
import json
import os
import shutil
import socket
from pathlib import Path


CACHES = (
    ("resources_servers/math_with_judge", "resources_servers/spreadsheet"),
    ("responses_api_agents/simple_agent", "responses_api_agents/spreadsheet_agent"),
)


def complete(path):
    return all((path / "bin" / name).is_file() for name in ("python", "activate"))


def prepare(root):
    root = root.resolve()
    plans = []
    # Validate both destinations before creating either one. Preserve existing
    # caches and incomplete evidence rather than merging or overwriting them.
    for base, module in CACHES:
        source, target = root / base / ".venv", root / module / ".venv"
        exists = target.exists() or target.is_symlink()
        if exists and (target.is_symlink() or not complete(target)):
            raise ValueError(f"Existing frontend cache is linked or incomplete; inspect: {target}")
        if not exists and not complete(source):
            raise ValueError(f"Existing base cache is missing or incomplete: {source}")
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
            if not complete(target):
                raise ValueError(f"Copied frontend cache is incomplete: {target}")
        result.append({"source": str(source), "target": str(target), "status": "reused" if exists else "copied"})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv-root", type=Path, required=True)
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
            receipt.update(caches=prepare(args.venv_root), status="prepared")
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
