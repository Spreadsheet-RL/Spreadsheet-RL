"""Repair moved Gym venv entrypoints from an inspection report, with rollback.

Only bin/activate{,.csh,.fish} and text console scripts whose shebang names the
old venv are eligible. Package files, configs and historical smoke are untouched.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile


def sha(body):
    return hashlib.sha256(body).hexdigest()


def replace(path, body):
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if os.geteuid() == 0:
                info = path.stat()
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def plan(report):
    rows = report["cached_venvs"]
    if len(rows) != 3 or len({row["venv"] for row in rows}) != 3:
        raise ValueError("Require exactly three distinct audited Gym venvs")
    changes = []
    for row in rows:
        root = Path(row["venv"])
        old = row.get("activation_virtual_env")
        if not old or not Path(old).is_absolute() or str(root) == old:
            raise ValueError("Require a distinct absolute old activation prefix")
        if not row.get("python_prefix_matches_location") or row.get("returncode") != 0:
            raise ValueError("The inspected venv interpreter did not pass")
        if root.is_symlink() or (root / "bin").is_symlink() or not root.is_absolute():
            raise ValueError("Require explicit non-symlink venv roots")
        for path in sorted((root / "bin").iterdir()):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            before = path.read_bytes()
            prefix = old.encode()
            if prefix not in before:
                continue
            shebang = before.split(b"\n", 1)[0]
            trampoline = shebang == b"#!/bin/sh" and re.search(
                rb"^'''exec' [\"']?" + re.escape(prefix) + rb"/bin/python",
                before[:2048],
                re.MULTILINE,
            )
            eligible = (
                path.name in {"activate", "activate.csh", "activate.fish"}
                or shebang.startswith(b"#!" + prefix + b"/bin/python")
                or trampoline
            )
            if eligible:
                before.decode("utf-8")
                changes.append((path, before, before.replace(prefix, str(root).encode())))
        if not any(path == root / "bin/activate" for path, _, _ in changes):
            raise ValueError("Expected stale bin/activate was not found; input may have changed")
    return changes


def verify_activation(root):
    code = 'import json,sys; print("VENV_PREFIX="+json.dumps(sys.prefix))'
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'source "$1/bin/activate" >/dev/null 2>&1 || exit 125; exec python -I -B -c "$2"',
            "venv-prefix-check",
            str(root),
            code,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"},
    )
    prefixes = [
        json.loads(line.split("=", 1)[1]) for line in result.stdout.splitlines() if line.startswith("VENV_PREFIX=")
    ]
    if result.returncode or not prefixes or Path(prefixes[-1]).resolve() != root.resolve():
        raise ValueError("Activation did not select the expected relocated interpreter")


def restore(receipt):
    files = receipt["files"]
    for item in files:
        if sha(Path(item["path"]).read_bytes()) != item["after_sha256"]:
            raise ValueError("Refusing rollback: an entrypoint changed after repair")
        if sha(Path(item["backup"]).read_bytes()) != item["before_sha256"]:
            raise ValueError("Backup integrity mismatch")
    for item in files:
        replace(Path(item["path"]), Path(item["backup"]).read_bytes())


def apply(report, changes, backup_dir):
    backup_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    receipt = {"hostname": socket.gethostname(), "status": "prepared", "files": []}
    receipt_path = backup_dir / "receipt.json"
    for index, (path, before, after) in enumerate(changes):
        if path.read_bytes() != before:
            raise ValueError("Entrypoint changed during planning")
        backup = backup_dir / (str(index) + ".bak")
        backup.write_bytes(before)
        backup.chmod(0o600)
        if backup.read_bytes() != before:
            raise ValueError("Backup verification failed")
        receipt["files"].append(
            dict(path=str(path), backup=str(backup), before_sha256=sha(before), after_sha256=sha(after))
        )
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_path.chmod(0o600)
    written = []
    try:
        for path, before, after in changes:
            if path.read_bytes() != before:
                raise ValueError("Entrypoint changed before replacement")
            written.append((path, before))
            replace(path, after)
        for row in report["cached_venvs"]:
            verify_activation(Path(row["venv"]))
    except Exception:
        for path, before in reversed(written):
            replace(path, before)
        receipt["status"] = "rolled_back"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    receipt["status"] = "repaired"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    if args.rollback:
        receipt = json.loads(args.rollback.read_text())
        if receipt["hostname"] != args.expected_hostname or receipt["status"] != "repaired":
            parser.error("Receipt is not a completed repair on this node")
        restore(receipt)
        receipt["status"] = "restored"
        args.rollback.write_text(json.dumps(receipt, indent=2) + "\n")
        print("VENV_ENTRYPOINTS_RESTORED")
        return
    if not args.report or (args.apply and not args.backup_dir):
        parser.error("Require --report and, for --apply, a fresh --backup-dir")
    report = json.loads(args.report.read_text())
    if report["hostname"] != args.expected_hostname:
        parser.error("Inspection report is for a different node")
    changes = plan(report)
    print(json.dumps({"event": "VENV_ENTRYPOINT_PLAN", "files": [str(row[0]) for row in changes]}), flush=True)
    if args.apply:
        receipt = apply(report, changes, args.backup_dir)
        print(
            json.dumps({"event": "VENV_ENTRYPOINTS_REPAIRED", "receipt": str(receipt), "activation_checks": 3}),
            flush=True,
        )


if __name__ == "__main__":
    main()
