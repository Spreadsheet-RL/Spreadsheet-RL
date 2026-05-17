from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class JobKind(str, Enum):
    REWARD = "reward"
    RECALCULATE = "recalculate"


@dataclass
class JobRecord:
    job_id: str
    thread_dir: str
    gt_file: Path
    proc_file: Path
    answer_position: str
    kind: JobKind = JobKind.REWARD
    status: JobStatus = JobStatus.QUEUED
    created_at_s: float = field(default_factory=time.time)
    started_at_s: float | None = None
    finished_at_s: float | None = None
    reward: float | None = None
    msg: str = ""
