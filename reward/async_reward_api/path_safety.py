from __future__ import annotations

from pathlib import Path

from .config import _get_output_root, _get_recalculate_job_root
from .models import JobKind, JobRecord


def _resolve_path(path: Path) -> Path:
    return path.resolve()


def _path_exists(path: Path) -> bool:
    return path.exists()


def _mkdir_parents(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _unlink_missing_ok(path: Path) -> None:
    path.unlink(missing_ok=True)


def _resolve_for_root_check(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        return _resolve_for_root_check(path).is_relative_to(_resolve_for_root_check(root))
    except ValueError:
        return False


def _expected_reward_proc_file(job_id: str, *, gt_file: Path) -> Path:
    return gt_file.parent / ".async_reward_jobs" / f"output_{job_id}.xlsx"


def _is_expected_reward_proc_file(*, job_id: str, gt_file: Path, proc_file: Path) -> bool:
    if any(separator in job_id for separator in ("/", "\\", "\x00")):
        return False
    try:
        return proc_file.resolve(strict=False) == _expected_reward_proc_file(
            job_id,
            gt_file=gt_file,
        ).resolve(strict=False)
    except (OSError, ValueError):
        return False


def _expected_recalculate_proc_file(job_id: str, *, storage_root: Path | None = None) -> Path:
    root = storage_root if storage_root is not None else _get_recalculate_job_root()
    return root / job_id / "workbook.xlsx"


def _is_safe_recalculate_job_id(job_id: str) -> bool:
    return len(job_id) == 32 and all(ch in "0123456789abcdef" for ch in job_id)


def _legacy_recalculate_storage_root_from_proc_file(*, job_id: str, proc_file: Path) -> Path | None:
    if not _is_safe_recalculate_job_id(job_id):
        return None
    if proc_file.name.lower() != "workbook.xlsx" or proc_file.parent.name != job_id:
        return None
    storage_root = proc_file.parent.parent
    if not _is_expected_recalculate_proc_file(
        job_id=job_id,
        proc_file=proc_file,
        storage_root=storage_root,
    ):
        return None
    return storage_root


def _recalculate_storage_root_from_gt_file(
    *,
    job_id: str,
    gt_file: Path | None,
    proc_file: Path,
) -> Path | None:
    if gt_file is None:
        return None
    try:
        gt_resolved = gt_file.resolve(strict=False)
        proc_resolved = proc_file.resolve(strict=False)
    except (OSError, ValueError):
        return None
    if gt_resolved == proc_resolved:
        return _legacy_recalculate_storage_root_from_proc_file(job_id=job_id, proc_file=proc_file)
    if _is_safe_recalculate_job_id(job_id) and gt_resolved == proc_resolved.parent.parent:
        if _is_expected_recalculate_proc_file(
            job_id=job_id,
            proc_file=proc_file,
            storage_root=gt_file,
        ):
            return gt_file
    return None


def _is_expected_recalculate_proc_file(
    *,
    job_id: str,
    proc_file: Path,
    storage_root: Path | None = None,
) -> bool:
    if not _is_safe_recalculate_job_id(job_id):
        return False
    try:
        return proc_file.resolve(strict=False) == _expected_recalculate_proc_file(
            job_id,
            storage_root=storage_root,
        ).resolve(strict=False)
    except (OSError, ValueError):
        return False


def _persisted_reward_job_paths_are_safe(job: JobRecord) -> bool:
    if not _is_expected_reward_proc_file(
        job_id=job.job_id,
        gt_file=job.gt_file,
        proc_file=job.proc_file,
    ):
        return False
    try:
        output_root_resolved = _get_output_root().resolve(strict=False)
        gt_resolved = job.gt_file.resolve(strict=False)
        proc_resolved = job.proc_file.resolve(strict=False)
    except (OSError, ValueError):
        return False
    return gt_resolved.is_relative_to(output_root_resolved) and proc_resolved.is_relative_to(
        output_root_resolved
    )


def _persisted_recalculate_job_paths_are_safe(job: JobRecord) -> bool:
    storage_root = _recalculate_storage_root_from_gt_file(
        job_id=job.job_id,
        gt_file=job.gt_file,
        proc_file=job.proc_file,
    )
    if not _is_expected_recalculate_proc_file(
        job_id=job.job_id,
        proc_file=job.proc_file,
        storage_root=storage_root,
    ):
        return False
    return _resolve_for_root_check(job.proc_file).is_relative_to(
        _resolve_for_root_check(_get_recalculate_job_root())
    )


def _persisted_job_paths_are_safe(job: JobRecord) -> bool:
    if job.kind is JobKind.REWARD:
        return _persisted_reward_job_paths_are_safe(job)
    if job.kind is JobKind.RECALCULATE:
        return _persisted_recalculate_job_paths_are_safe(job)
    return False
