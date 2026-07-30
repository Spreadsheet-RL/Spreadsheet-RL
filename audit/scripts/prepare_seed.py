from __future__ import annotations

import json
import os
import random
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "audit.config.json").read_text(encoding="utf-8"))
ARCHIVE = Path(
    os.environ.get("SPREADSHEET_RL_ARCHIVE", ROOT.parent / "data" / "spreadsheets.zip")
)
PUBLIC_WORKBOOKS = ROOT / "frontend" / "public" / "workbooks"
SEED_DIR = ROOT / "seed"
POOL_SIZE = int(CONFIG["pool_size_per_split"])
CORE_ASSIGNMENTS_PER_SPLIT = int(CONFIG["core_assignments_per_split"])
BASE_SEED = str(CONFIG["seed"])


def normalize_email(value: object) -> str:
    email = str(value).strip().lower()
    if not email or "@" not in email:
        raise ValueError(f"Invalid auditor email: {value!r}")
    return email


CORE_AUDITORS = [normalize_email(email) for email in CONFIG["core_auditors"]]
ADDITIONAL_AUDITOR_ITEMS = [
    (normalize_email(email), splits)
    for email, splits in CONFIG["additional_auditors"].items()
]
AUDITORS = [*CORE_AUDITORS, *(email for email, _ in ADDITIONAL_AUDITOR_ITEMS)]
if len(AUDITORS) != len(set(AUDITORS)):
    raise ValueError("Auditor emails must be unique after normalization")
ADDITIONAL_AUDITORS = dict(ADDITIONAL_AUDITOR_ITEMS)
ASSIGNMENT_TARGETS = {
    **{
        email: {"training": CORE_ASSIGNMENTS_PER_SPLIT, "domain": CORE_ASSIGNMENTS_PER_SPLIT}
        for email in CORE_AUDITORS
    },
    **{
        email: {split: config["count"] for split, config in splits.items()}
        for email, splits in ADDITIONAL_AUDITORS.items()
    },
}


def sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def eligible_task_dirs(names: set[str], prefix: str) -> list[str]:
    suffix = "/instruction.json"
    task_dirs = []
    for name in names:
        if not name.startswith(prefix + "/") or not name.endswith(suffix):
            continue
        task_dir = name[: -len(suffix)]
        required = {f"{task_dir}/output.xlsx", f"{task_dir}/target.xlsx"}
        if required.issubset(names):
            task_dirs.append(task_dir)
    return sorted(task_dirs)


def choose_pool(candidates: list[str], split: str) -> list[str]:
    rng = random.Random(f"{BASE_SEED}:pool:{split}")
    return rng.sample(candidates, POOL_SIZE)


def build_assignments(task_ids: list[str], split: str) -> dict[str, list[str]]:
    if len(task_ids) != POOL_SIZE:
        raise ValueError(f"{split} pool must contain {POOL_SIZE} tasks")
    shuffled = list(task_ids)
    random.Random(f"{BASE_SEED}:omissions:{split}").shuffle(shuffled)
    omission_size = POOL_SIZE - CORE_ASSIGNMENTS_PER_SPLIT
    if omission_size * len(CORE_AUDITORS) != POOL_SIZE:
        raise ValueError("Core auditor omissions must partition the full task pool")
    assignments: dict[str, list[str]] = {}
    for index, email in enumerate(CORE_AUDITORS):
        omitted = set(shuffled[index * omission_size : (index + 1) * omission_size])
        assigned = [task_id for task_id in task_ids if task_id not in omitted]
        random.Random(f"{BASE_SEED}:order:{split}:{email}").shuffle(assigned)
        assignments[email] = assigned
    for email, split_configs in ADDITIONAL_AUDITORS.items():
        split_config = split_configs[split]
        ranked = sorted(
            enumerate(task_ids, start=1),
            key=lambda item: ((item[0] * 37 + split_config["offset"]) % POOL_SIZE, item[0]),
        )
        assignments[email] = [task_id for _, task_id in ranked[: split_config["count"]]]
    return assignments


def main() -> None:
    if not ARCHIVE.is_file():
        raise FileNotFoundError(f"Dataset archive not found: {ARCHIVE}")

    if PUBLIC_WORKBOOKS.exists():
        shutil.rmtree(PUBLIC_WORKBOOKS)
    PUBLIC_WORKBOOKS.mkdir(parents=True)
    SEED_DIR.mkdir(parents=True, exist_ok=True)

    tasks: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    with zipfile.ZipFile(ARCHIVE) as archive:
        names = set(archive.namelist())
        pools = {
            "training": choose_pool(eligible_task_dirs(names, "excelforum"), "training"),
            "domain": choose_pool(eligible_task_dirs(names, "domain"), "domain"),
        }

        split_task_ids: dict[str, list[str]] = {}
        for split, source_dirs in pools.items():
            split_task_ids[split] = []
            for pool_index, source_dir in enumerate(source_dirs, start=1):
                task_id = f"{split}-{pool_index:03d}"
                split_task_ids[split].append(task_id)
                instruction = json.loads(archive.read(f"{source_dir}/instruction.json"))
                path_parts = source_dir.split("/")
                category = path_parts[1] if len(path_parts) > 2 else "unknown"
                output_key = f"workbooks/{task_id}/output.xlsx"
                target_key = f"workbooks/{task_id}/target.xlsx"
                task = {
                    "id": task_id,
                    "split": split,
                    "pool_index": pool_index,
                    "category": category,
                    "source_path": source_dir,
                    "instruction": str(instruction.get("instruction") or "").strip(),
                    "answer_position": str(instruction.get("answer_position") or "").strip(),
                    "output_key": output_key,
                    "target_key": target_key,
                }
                if not task["instruction"] or not task["answer_position"]:
                    raise ValueError(f"Missing audit metadata in {source_dir}")
                tasks.append(task)

                destination = PUBLIC_WORKBOOKS / task_id
                destination.mkdir()
                for kind in ("output", "target"):
                    source_name = f"{source_dir}/{kind}.xlsx"
                    (destination / f"{kind}.xlsx").write_bytes(archive.read(source_name))

        assignments_by_split = {
            split: build_assignments(split_task_ids[split], split)
            for split in ("training", "domain")
        }
        for split in ("training", "domain"):
            base_order = 0 if split == "training" else CORE_ASSIGNMENTS_PER_SPLIT
            for email in CORE_AUDITORS:
                assigned_ids = assignments_by_split[split][email]
                for index, task_id in enumerate(assigned_ids, start=1):
                    assignments.append(
                        {
                            "email": email,
                            "task_id": task_id,
                            "assignment_order": base_order + index,
                        }
                    )
        for email in ADDITIONAL_AUDITORS:
            assignment_order = 0
            for split in ("training", "domain"):
                for index, task_id in enumerate(assignments_by_split[split][email], start=1):
                    assignments.append(
                        {
                            "email": email,
                            "task_id": task_id,
                            "assignment_order": assignment_order + index,
                        }
                    )
                assignment_order += len(assignments_by_split[split][email])

    task_counts = {split: sum(task["split"] == split for task in tasks) for split in ("training", "domain")}
    if task_counts != {"training": POOL_SIZE, "domain": POOL_SIZE}:
        raise AssertionError(f"Unexpected task counts: {task_counts}")
    for split in ("training", "domain"):
        for email in AUDITORS:
            count = sum(
                item["email"] == email and str(item["task_id"]).startswith(f"{split}-")
                for item in assignments
            )
            expected = ASSIGNMENT_TARGETS[email][split]
            if count != expected:
                raise AssertionError(f"{email} has {count} {split} assignments")
        review_counts = [
            sum(item["task_id"] == task["id"] for item in assignments)
            for task in tasks
            if task["split"] == split
        ]
        expected_reviews = sum(targets[split] for targets in ASSIGNMENT_TARGETS.values())
        if sum(review_counts) != expected_reviews or min(review_counts) < 1:
            raise AssertionError(f"Unexpected {split} review counts: {review_counts}")

    workbook_count = len(list(PUBLIC_WORKBOOKS.glob("*/*.xlsx")))
    if workbook_count != len(tasks) * 2:
        raise AssertionError(f"Expected {len(tasks) * 2} workbooks, found {workbook_count}")

    manifest = {
        "version": 1,
        "seed": BASE_SEED,
        "pool_size_per_split": POOL_SIZE,
        "config": CONFIG,
        "assignment_targets": ASSIGNMENT_TARGETS,
        "auditors": AUDITORS,
        "tasks": tasks,
        "assignments": assignments,
    }
    (SEED_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    sql_lines = ["DELETE FROM audits;", "DELETE FROM assignments;", "DELETE FROM tasks;"]
    for task in tasks:
        columns = (
            "id",
            "split",
            "pool_index",
            "category",
            "source_path",
            "instruction",
            "answer_position",
            "output_key",
            "target_key",
        )
        values = [
            "NULL" if task[column] is None else sql_text(str(task[column]))
            for column in columns
        ]
        values[2] = str(task["pool_index"])
        sql_lines.append(f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({', '.join(values)});")
    for item in assignments:
        sql_lines.append(
            "INSERT INTO assignments (email, task_id, assignment_order) VALUES "
            f"({sql_text(str(item['email']))}, {sql_text(str(item['task_id']))}, {item['assignment_order']});"
        )
    sql_lines.append("")
    (SEED_DIR / "seed.sql").write_text("\n".join(sql_lines), encoding="utf-8")

    print(
        json.dumps(
            {
                "tasks": len(tasks),
                "assignments": len(assignments),
                "workbooks": workbook_count,
                "per_task_auditors": {
                    str(count): sum(
                        sum(item["task_id"] == task["id"] for item in assignments) == count
                        for task in tasks
                    )
                    for count in range(4, 7)
                },
            }
        )
    )


if __name__ == "__main__":
    main()
