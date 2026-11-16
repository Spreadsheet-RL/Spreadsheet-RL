"""Convert one parser-specific parquet per split to Gym JSONL and a private catalog.

Run where the dataset already lives. Never download workbook data through the Mac.
"""

import argparse
import json
from pathlib import Path

from resources_servers.spreadsheet.schemas import tool_schemas
from resources_servers.spreadsheet.service.catalog import sha256_file, under


SYSTEM_PROMPT = """You edit data.xlsx to satisfy the user's requested workbook end state.
The workbook is the answer; a text-only solution does not complete the task.
Work only on data.xlsx. Use the provided workbook overview and target regions.
Inspect a small relevant region when an edit depends on its contents. Do not
invent data or repeat a successful edit. Use fill_formula for formulas and
write_range for static values. Sheet names are case-sensitive.
Native tools run with openpyxl; formula recalculation uses LibreOffice Calc.
fill_formula, clear_range, delete_rows and delete_columns recalculate before
commit. write_range does not refresh dependent formula values. After Python
changes formulas or their precedent cells, call recalculate_and_read.
Manage-sheet renames/deletions and row/column deletion do not guarantee automatic
formula-reference repair; update affected formulas when necessary.
Python runs in a fresh isolated process for each call. Save modifications to
data.xlsx; file contents persist, Python variables do not. Use openpyxl for
workbook edits. Use code_interpreter when a native tool cannot express the edit.
Use the supplied function tools. Submit dependent operations sequentially.
After a deterministic error, diagnose before retrying; do not repeat unchanged
calls indefinitely. Finish when the requested workbook state is achieved.
"""


def prepare(parquets, data_root, output_dir, revision, skip_missing=False):
    import pyarrow.parquet as pq

    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / (parquet.stem + ".jsonl") for parquet in parquets]
    targets += [output_dir / "manifest.unqualified.jsonl", output_dir / "preparation.json"]
    if len(targets) != len(set(targets)) or any(path.exists() for path in targets):
        raise FileExistsError("Preparation output exists or input stems collide")
    manifest, missing, seen = [], [], set()
    for parquet in parquets:
        rows = []
        for row in pq.read_table(parquet).to_pylist():
            task_id = row["extra_info"]["id"]
            if task_id in seen:
                raise ValueError("Duplicate task ID: select only one parser variant per split")
            seen.add(task_id)
            thread = row["reward_model"]["ground_truth"]
            try:
                seed, oracle = under(data_root, f"{thread}/output.xlsx"), under(data_root, f"{thread}/target.xlsx")
            except ValueError:
                if not skip_missing:
                    raise
                missing.append(task_id)
                continue
            answer = row["extra_info"]["answer_position"]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [
                message for message in row["prompt"] if message["role"] == "user"
            ]
            rows.append(
                {
                    "task_id": task_id,
                    "dataset_revision": revision,
                    "responses_create_params": {
                        "input": messages,
                        "tools": tool_schemas(),
                        "parallel_tool_calls": False,
                    },
                }
            )
            manifest.append(
                {
                    "task_id": task_id,
                    "seed": f"{thread}/output.xlsx",
                    "oracle": f"{thread}/target.xlsx",
                    "answer_position": answer,
                    "dataset_revision": revision,
                    "seed_sha256": sha256_file(seed),
                    "oracle_sha256": sha256_file(oracle),
                    "libreoffice_qualified": False,
                }
            )
        target = output_dir / (parquet.stem + ".jsonl")
        if target.exists():
            raise FileExistsError(target)
        target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    (output_dir / "manifest.unqualified.jsonl").write_text("".join(json.dumps(row) + "\n" for row in manifest))
    (output_dir / "preparation.json").write_text(
        json.dumps({"revision": revision, "tasks": len(manifest), "missing_task_ids": missing}, indent=2)
    )
    return len(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, action="append", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="Pinned Hugging Face dataset commit")
    parser.add_argument("--skip-missing", action="store_true", help="Record missing workbook tasks explicitly")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "prepared_tasks": prepare(
                    args.parquet, args.data_root, args.output_dir, args.revision, args.skip_missing
                )
            }
        )
    )


if __name__ == "__main__":
    main()
