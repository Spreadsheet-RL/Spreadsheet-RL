"""Add Gym agent routing to qualified JSONL and optionally change only its train data path.

Uses only the standard library. No workbook recalculation, runtime changes or training.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from uuid import uuid4


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def convert_row(row, agent_name):
    if not isinstance(row.get("task_id"), str) or not row["task_id"]:
        raise ValueError("Spreadsheet row requires a nonempty task_id")
    params = row.get("responses_create_params")
    if not isinstance(params, dict) or not params.get("input") or not params.get("tools"):
        raise ValueError("Spreadsheet row requires responses_create_params.input and tools")
    if "rollout_id" in row or "session_id" in row:
        raise ValueError("Dataset rows must not contain rollout/session identities")
    reference = {"name": agent_name, "type": "responses_api_agents"}
    previous = row.get("agent_ref")
    if previous is not None and previous not in (reference, {"name": agent_name}):
        raise ValueError("Existing agent_ref refers to a different or unsupported agent")
    return dict(row, agent_ref=reference)


def convert(input_dir, output_dir, agent_name):
    paths = {split: input_dir / f"{split}.qualified.jsonl" for split in ("train", "test")}
    identity = {
        "format_version": 1,
        "agent_name": agent_name,
        "inputs": {split: digest(path) for split, path in paths.items()},
    }
    if output_dir.exists():
        receipt = json.loads((output_dir / "conversion.json").read_text())
        if receipt["identity"] != identity:
            raise ValueError("Existing converted data has different input hashes or agent")
        if any(digest(output_dir / (split + ".jsonl")) != receipt["outputs"][split] for split in paths):
            raise ValueError("Existing converted data checksum mismatch")
        return receipt
    stage = Path(tempfile.mkdtemp(prefix=".gym-conversion-", dir=output_dir.parent))
    seen, counts = set(), {}
    for split, source in paths.items():
        count = 0
        with source.open() as stream, (stage / (split + ".jsonl")).open("w") as target:
            for line in stream:
                row = convert_row(json.loads(line), agent_name)
                if row["task_id"] in seen:
                    raise ValueError("Duplicate task_id across converted train/test splits")
                seen.add(row["task_id"])
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        if not count:
            raise ValueError("A qualified split is empty")
        counts[split] = count
    if any(digest(path) != identity["inputs"][split] for split, path in paths.items()):
        raise ValueError("Input data changed during conversion")
    receipt = {
        "identity": identity,
        "rows": counts,
        "outputs": {split: digest(stage / (split + ".jsonl")) for split in paths},
    }
    (stage / "conversion.json").write_text(json.dumps(receipt, indent=2) + "\n")
    stage.rename(output_dir)
    return receipt


def patch_train_path(config, new_path):
    """Preserve the YAML byte-for-byte except the single data.train data_path scalar."""
    if config.is_symlink():
        raise ValueError("Trainer YAML is a symlink; select its intended target explicitly before editing")
    original = config.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    data = [i for i, line in enumerate(lines) if re.fullmatch(r"data:[ \t]*(?:#.*)?\r?\n?", line)]
    if len(data) != 1:
        raise ValueError("Expected one top-level data mapping")
    end = next((i for i in range(data[0] + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
    train = [i for i in range(data[0] + 1, end) if re.fullmatch(r"  train:[ \t]*(?:#.*)?\r?\n?", lines[i])]
    if len(train) != 1:
        raise ValueError("Expected data.train as an indented dataset list")
    stop = next((i for i in range(train[0] + 1, end) if re.match(r"^  [^\s#]", lines[i])), end)
    matches = []
    for i in range(train[0] + 1, stop):
        match = re.fullmatch(r"([ \t]+-[ \t]+data_path:[ \t]*)([^\r\n]*)(\r?\n)?", lines[i])
        if match:
            matches.append((i, match))
    if len(matches) != 1:
        raise ValueError("Expected a single data.train entry starting with data_path; refusing an ambiguous patch")
    index, match = matches[0]
    scalar, *comment = re.split(r"[ \t]+(?=#)", match[2], maxsplit=1)
    scalar = scalar.strip()
    previous = json.loads(scalar) if scalar.startswith('"') else scalar.strip("'").replace("''", "'")
    if previous == str(new_path):
        return None
    lines[index] = match[1] + json.dumps(str(new_path)) + ("  " + comment[0] if comment else "") + (match[3] or "")
    backup = config.with_name(config.name + ".before-spreadsheet-" + uuid4().hex[:12] + ".bak")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(original)
    info = config.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".spreadsheet-data-", dir=config.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write("".join(lines).encode("utf-8"))
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
        if config.read_bytes() != original:
            raise ValueError("Trainer YAML changed concurrently; its edits were preserved")
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)
    return str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agent-name", default="spreadsheet_agent")
    parser.add_argument("--trainer-config", type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = convert(args.input_dir.resolve(), output, args.agent_name)
    backup = patch_train_path(args.trainer_config, output / "train.jsonl") if args.trainer_config else None
    print(
        json.dumps(
            {
                "event": "SP10_DATA_READY",
                "rows": receipt["rows"],
                "agent_name": args.agent_name,
                "train_data_path": str(output / "train.jsonl"),
                "test_data_path": str(output / "test.jsonl"),
                "trainer_config": str(args.trainer_config) if args.trainer_config else None,
                "backup": backup,
            }
        )
    )


if __name__ == "__main__":
    main()
