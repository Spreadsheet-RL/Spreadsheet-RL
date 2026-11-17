"""Replace the judge-copy call only in an already-generated Spreadsheet launcher."""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys


ANCHOR = 'bash "${NEMO_RL_UTILS_DIR}/copy_gym_venvs.sh" "${CONFIG_PATH}"'


def validation_command(validator, helpers):
    import shlex

    return (
        "python -B "
        + shlex.quote(str(validator))
        + " --helpers-root "
        + shlex.quote(str(helpers))
        + ' --config "${CONFIG_PATH}" || exit $?'
    )


def patch(text, validator, helpers):
    if "# Spreadsheet full-training IPC" not in text or text.count(ANCHOR) != 1:
        raise ValueError("Require the generated Spreadsheet launcher with exactly one legacy copy call")
    return text.replace(ANCHOR, validation_command(validator, helpers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("launcher", "original-launcher", "validator", "helpers-root", "backup-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    if args.launcher.resolve() == args.original_launcher.resolve() or args.launcher.is_symlink():
        parser.error("Require a separate regular generated launcher")
    if not args.validator.is_file():
        parser.error("Cache validator is missing")
    sys.path.insert(0, str(args.helpers_root))
    from prepare_existing_launcher import replace_atomic

    before = args.launcher.read_bytes()
    original = args.original_launcher.read_bytes()
    after = patch(before.decode(), args.validator.resolve(), args.helpers_root.resolve()).encode()
    subprocess.run(["bash", "-n"], input=after, check=True)
    args.backup_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    backup = args.backup_dir / "launcher.before.sh"
    backup.write_bytes(before)
    backup.chmod(0o600)
    if backup.read_bytes() != before or args.launcher.read_bytes() != before:
        raise ValueError("Backup/input integrity check failed")
    if args.original_launcher.read_bytes() != original:
        raise ValueError("Original launcher changed during preflight")
    receipt = dict(
        launcher=str(args.launcher),
        backup=str(backup),
        before_sha256=hashlib.sha256(before).hexdigest(),
        after_sha256=hashlib.sha256(after).hexdigest(),
        original_launcher=str(args.original_launcher),
        original_sha256=hashlib.sha256(original).hexdigest(),
        validator=str(args.validator.resolve()),
        validator_sha256=hashlib.sha256(args.validator.read_bytes()).hexdigest(),
        status="prepared",
    )
    receipt_path = args.backup_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_path.chmod(0o600)
    try:
        replace_atomic(args.launcher, after)
        if args.original_launcher.read_bytes() != original:
            raise ValueError("Original launcher changed during update")
    except Exception:
        replace_atomic(args.launcher, before)
        raise
    receipt["status"] = "patched"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "SPREADSHEET_LAUNCHER_CACHE_FIXED",
                "receipt": str(receipt_path),
                "original_launcher_unchanged": True,
                "training_started": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
