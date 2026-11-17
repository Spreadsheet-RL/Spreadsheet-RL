"""Measure dataset attrition without changing scoring or reading workbook values."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def range_bin(address):
    from resources_servers.spreadsheet.service.workbook import split_ranges

    try:
        size = len(split_ranges(address, max_ranges=4096))
    except ValueError:
        return "invalid_metadata"
    return "1" if size == 1 else "2-5" if size <= 5 else "6-20" if size <= 20 else "21+"


def analyze(tasks, rejected, summary):
    ids = [task["task_id"] for task in tasks]
    failures = {row["task_id"]: row for row in rejected}
    if len(ids) != len(set(ids)) or len(failures) != len(rejected) or not set(failures).issubset(ids):
        raise ValueError("Duplicate or unknown task IDs in coverage inputs")
    if len(tasks) != summary["checked"] or len(rejected) != summary["baseline_rejected"]:
        raise ValueError("Coverage inputs do not match completed audit")
    groups = {
        name: defaultdict(lambda: {"total": 0, "baseline_kept": 0, "strict_recovered": 0, "still_withheld": 0})
        for name in ("split", "source", "answer_ranges")
    }
    for task in tasks:
        failure = failures.get(task["task_id"])
        recovered = bool(failure and failure["verdict"] == "recovered_strict")
        keys = {
            "split": task["split"],
            "source": "/".join(task["task_id"].split("/")[:2]),
            "answer_ranges": range_bin(task["answer_position"]),
        }
        for dimension, key in keys.items():
            bucket = groups[dimension][key]
            bucket["total"] += 1
            bucket["baseline_kept"] += failure is None
            bucket["strict_recovered"] += recovered
            bucket["still_withheld"] += bool(failure and not recovered)
    for dimension in groups.values():
        for bucket in dimension.values():
            bucket["baseline_keep_rate"] = bucket["baseline_kept"] / bucket["total"]
            bucket["keep_rate_after_strict_recovery"] = (
                bucket["baseline_kept"] + bucket["strict_recovered"]
            ) / bucket["total"]
    return {
        "schema_version": 1,
        "audit_source_commit": summary["identity"]["source_commit"],
        "groups": {name: dict(sorted(group.items())) for name, group in groups.items()},
        "limitation": "Descriptive coverage only; zero reward regressions does not prove absence of selection bias. No model evaluation, formula-level representativeness, or Excel equivalence is inferred.",
        "active_training_data_modified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tasks", "rejected", "summary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a fresh coverage output path")
    tasks = [json.loads(line) for line in args.tasks.read_text().splitlines()]
    rejected = [json.loads(line) for line in args.rejected.read_text().splitlines()]
    result = analyze(tasks, rejected, json.loads(args.summary.read_text()))
    result["input_sha256"] = {
        name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest() for name in ("tasks", "rejected", "summary")
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"event": "selection_coverage_ready", "splits": result["groups"]["split"]}), flush=True)


if __name__ == "__main__":
    main()
