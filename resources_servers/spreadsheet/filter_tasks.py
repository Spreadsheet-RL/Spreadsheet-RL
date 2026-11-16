"""Keep the training/evaluation JSONL aligned with the qualified service catalog."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "input", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    catalog = {row["task_id"]: row for line in args.manifest.read_text().splitlines() if (row := json.loads(line))}
    kept, excluded = [], 0
    for line in args.input.read_text().splitlines():
        row = json.loads(line)
        task = catalog.get(row["task_id"])
        if task is None or not task.get("libreoffice_qualified"):
            excluded += 1
            continue
        if row["dataset_revision"] != task["dataset_revision"]:
            raise ValueError("Dataset and service catalog revisions differ")
        kept.append(row)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept))
    print(json.dumps({"kept": len(kept), "excluded": excluded}))


if __name__ == "__main__":
    main()
