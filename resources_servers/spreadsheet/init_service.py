"""Create private service/adapter configuration on the service node, not in a release."""

import argparse
import json
import os
import secrets
from pathlib import Path


def write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("data-root", "manifest", "state-root", "python-rootfs", "config-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--service-url", required=True, help="Address reachable from the training container")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    destination = args.config_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("api-key", "service.json", "gym.private.yaml"):
        if (destination / name).exists():
            raise FileExistsError("Private configuration already exists; refusing to replace it")
    key = secrets.token_urlsafe(48)
    write_private(destination / "api-key", key + "\n")
    config = {
        "host": "0.0.0.0",
        "port": args.port,
        "data_root": str(args.data_root.resolve()),
        "manifest": str(args.manifest.resolve()),
        "state_root": str(args.state_root.resolve()),
        "api_key_file": str(destination / "api-key"),
        "python_isolation": "chroot_uid_seccomp",
        "python_rootfs": str(args.python_rootfs.resolve()),
    }
    write_private(destination / "service.json", json.dumps(config, indent=2))
    private = (
        "spreadsheet_resources:\n  resources_servers:\n    spreadsheet:\n"
        f"      service_url: {json.dumps(args.service_url)}\n"
        f"      api_key: {json.dumps(key)}\n"
        f"      session_secret_key: {json.dumps(secrets.token_urlsafe(48))}\n"
    )
    write_private(destination / "gym.private.yaml", private)
    print("Created private service.json and gym.private.yaml. No secrets were printed.")


if __name__ == "__main__":
    main()
