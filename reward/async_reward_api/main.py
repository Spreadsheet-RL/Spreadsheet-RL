"""Backward-compat re-export/composition shim; the ASGI entry point is `async_reward_api.main:app`.

This module re-exports a non-exhaustive convenience set of primary symbols for older imports.
Many internal helpers now live only in `config`, `path_safety`, `sample_meta`, `aio`,
`windows_process`, and `manager`, and should be imported from those owning modules directly.
Re-exported names are the same objects for reads/imports, but rebinding here does not
propagate; patch the owning modules instead. `async_reward_api.main` is not a monkeypatch
target.
"""

from __future__ import annotations

# Re-exports are the compatibility surface of this shim.
# ruff: noqa: F401

from .aio import (
    UploadTooLargeError,
    _await_shielded_ignoring_repeated_cancels,
    _await_worker_communicate_cleanup,
    _copy_upload_to_path,
    _copy_upload_to_path_cancellation_safe,
    _rmtree_with_retries,
    _run_cancellation_safe,
    _unlink_with_retries,
)
from .job_store import JobSnapshot, SqliteJobStore
from .manager import (
    ControlledWorkerExitedError,
    ExcelWorkerPool,
    ExcelWorkerProcess,
    _JOB_TASK_SHUTDOWN_TIMEOUT_S,
    RewardJobManager,
    WorkerExitedError,
    _cleanup_worker_excel_pid_file,
    _cleanup_worker_excel_pid_file_cancellation_safe,
    _delete_dir_contents,
    _enable_timeout_excel_fallback_kill,
    _get_windows_excel_diagnostics_dir,
    _is_safe_windows_excel_diagnostics_dir,
    _kill_excel_pid_from_file,
    _list_excel_pids,
    _kill_subprocess_tree,
    _parse_worker_stdout,
    _recalc_file_via_worker,
    _run_worker_subprocess,
    _run_worker_subprocess_job,
    _start_worker_subprocess,
    _compute_reward_via_worker,
)
from .models import JobKind, JobRecord, JobStatus
from .path_safety import (
    _expected_recalculate_proc_file,
    _expected_reward_proc_file,
    _is_expected_recalculate_proc_file,
    _is_expected_reward_proc_file,
    _is_safe_recalculate_job_id,
    _is_under_root,
    _legacy_recalculate_storage_root_from_proc_file,
    _mkdir_parents,
    _path_exists,
    _persisted_job_paths_are_safe,
    _persisted_recalculate_job_paths_are_safe,
    _persisted_reward_job_paths_are_safe,
    _recalculate_storage_root_from_gt_file,
    _resolve_for_root_check,
    _resolve_path,
    _unlink_missing_ok,
)
from .platform import Platform
from .routes import (
    _file_chunks,
    _finite_float_or_none,
    _get_snapshot_or_503,
    _open_binary,
    _public_job_msg,
    _validate_wait_s,
    _wait_for_terminal_snapshot,
    app,
    health,
    job_manager,
    lifespan,
    platform,
    recalculate_result,
    recalculate_status,
    recalculate_submit,
    recalculate_submit_compat,
    reward_result,
    reward_status,
    reward_submit,
    reward_submit_compat,
)
from .sample_meta import (
    _RewardSampleMetadata,
    _SAMPLE_META_CACHE,
    _SAMPLE_META_CACHE_LOCK,
    _SAMPLE_META_INFLIGHT,
    _SAMPLE_META_INFLIGHT_WAIT_S,
    _SampleMetadataInflight,
    _get_cached_sample_metadata,
    _resolve_sample_metadata,
)

__all__ = [name for name in globals() if not name.startswith('__')]
