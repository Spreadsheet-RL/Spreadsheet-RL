"""Server-owned task metadata; callers only submit opaque task IDs."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def under(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("Task path escapes its configured data root")
    if not path.is_file() or path.suffix != ".xlsx":
        raise ValueError("Task workbook is missing or is not .xlsx")
    return path


@dataclass(frozen=True)
class Task:
    task_id: str
    seed: Path
    oracle: Path
    answer_position: str
    dataset_revision: str
    seed_sha256: str
    oracle_sha256: str


class Catalog:
    def __init__(self, manifest: Path, data_root: Path, allow_unqualified: bool = False):
        self.tasks: dict[str, Task] = {}
        self.manifest_sha256 = sha256_file(manifest)
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not item.get("libreoffice_qualified") and not allow_unqualified:
                raise ValueError("Task lacks a LibreOffice compatibility qualification")
            task_id = item["task_id"]
            if task_id in self.tasks:
                raise ValueError("Duplicate task ID in manifest")
            seed, oracle = under(data_root, item["seed"]), under(data_root, item["oracle"])
            seed_hash, oracle_hash = sha256_file(seed), sha256_file(oracle)
            if seed_hash != item["seed_sha256"] or oracle_hash != item["oracle_sha256"]:
                raise ValueError("Dataset checksum does not match the manifest")
            if not item["answer_position"] or not item["dataset_revision"]:
                raise ValueError("Task needs answer regions and a dataset revision")
            self.tasks[task_id] = Task(
                task_id, seed, oracle, item["answer_position"], item["dataset_revision"], seed_hash, oracle_hash
            )
        if not self.tasks:
            raise ValueError("Empty task manifest")

    def get(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise ValueError("Unknown task ID") from None
