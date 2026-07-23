from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from .platform import Platform, detect_platform


def env_float(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    value = os.environ.get(name, str(default))
    try:
        result = float(value)
    except ValueError:
        return float(default)
    if not math.isfinite(result):
        return float(default)
    if lo is not None:
        result = max(float(lo), result)
    if hi is not None:
        result = min(result, float(hi))
    return result


def env_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    value = os.environ.get(name, str(default))
    try:
        result = int(value)
    except ValueError:
        return int(default)
    if lo is not None:
        result = max(int(lo), result)
    if hi is not None:
        result = min(result, int(hi))
    return result


def env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "n", "off"}:
        return False
    return default


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(os.path.expandvars(value)).expanduser()
    return default


def _get_output_root() -> Path:
    return env_path("REWARD_API_OUTPUT_ROOT", Path.home() / "async_reward_api_output")


def _get_db_path() -> Path:
    return env_path("REWARD_API_DB_PATH", Path.home() / ".async_reward_api" / "jobs.sqlite3")


def _get_sqlite_executor_workers() -> int:
    return env_int("REWARD_API_SQLITE_EXECUTOR_WORKERS", 2, lo=1, hi=8)


def _get_platform() -> Platform:
    return detect_platform()


def _get_windows_excel_diagnostics_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Temp" / "Diagnostics"
    return Path.home() / "AppData" / "Local" / "Temp" / "Diagnostics"


def _get_windows_excel_diagnostics_dir() -> Path | None:
    value = os.environ.get("REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR")
    if value is None or not value.strip():
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _get_worker_timeout_s() -> float:
    value = os.environ.get("REWARD_API_WORKER_TIMEOUT_S", "240")
    try:
        timeout_s = float(value)
    except ValueError:
        return 240.0
    if timeout_s <= 0 or not math.isfinite(timeout_s):
        return 240.0
    return timeout_s


def _get_instance_per_worker() -> int:
    if os.name != "nt":
        return 0
    value = os.environ.get("REWARD_API_INSTANCE_PER_WORKER")
    if value is None or not value.strip():
        return 1
    try:
        n = int(value)
    except ValueError:
        return 1
    return max(0, n)


def _get_job_ttl_s() -> float:
    return env_float("REWARD_API_JOB_TTL_S", 3600.0, lo=60.0)


def _get_max_queue_size() -> int:
    return env_int("REWARD_API_MAX_QUEUE_SIZE", 3000, lo=1)


def _get_cleanup_batch_size() -> int:
    return env_int("REWARD_API_CLEANUP_BATCH_SIZE", 512, lo=1, hi=10000)


def _get_cleanup_max_batches() -> int:
    return env_int("REWARD_API_CLEANUP_MAX_BATCHES", 8, lo=1, hi=1000)


def _get_cleanup_leader_lease_s() -> float:
    return env_float("REWARD_API_CLEANUP_LEADER_LEASE_S", 900.0, lo=30.0, hi=3600.0)


def _get_stale_sweep_leader_lease_s() -> float:
    return env_float("REWARD_API_STALE_SWEEP_LEADER_LEASE_S", 30.0, lo=5.0, hi=300.0)


def _get_quarantine_sweep_interval_s() -> float:
    return env_float("REWARD_API_QUARANTINE_SWEEP_INTERVAL_S", 600.0, lo=0.0, hi=86400.0)


def _get_health_failure_ttl_s() -> float:
    return env_float("REWARD_API_HEALTH_FAILURE_TTL_S", 600.0, lo=0.0, hi=86400.0)


def _get_cleanup_retry_after_s() -> float:
    return env_float("REWARD_API_CLEANUP_RETRY_AFTER_S", 300.0, lo=1.0, hi=3600.0)


def _get_cleanup_retry_max_s() -> float:
    return env_float("REWARD_API_CLEANUP_RETRY_MAX_S", 3600.0, lo=60.0, hi=86400.0)


def _get_cleanup_retry_batch_share() -> float:
    return env_float("REWARD_API_CLEANUP_RETRY_BATCH_SHARE", 0.25, lo=0.0, hi=1.0)


def _get_idle_poll_max_s(base_poll_s: float) -> float:
    value = env_float(
        "REWARD_API_IDLE_POLL_MAX_S",
        0.5,
        hi=30.0,
    )
    return min(max(float(base_poll_s), value), 30.0)


def _get_poll_interval_s() -> float:
    return env_float("REWARD_API_POLL_INTERVAL_S", 0.2, lo=0.05, hi=5.0)


def _parse_finite_env_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return result


def _parse_result_poll_interval_env() -> float | None:
    return _parse_finite_env_float("REWARD_API_RESULT_POLL_INTERVAL_S")


def _get_result_poll_interval_s() -> float:
    poll_s = _parse_result_poll_interval_env()
    if poll_s is None:
        return _get_poll_interval_s()
    return min(max(0.05, poll_s), 5.0)


def _get_default_result_poll_max_s(base_poll_s: float) -> float:
    if (
        _parse_result_poll_interval_env() is None
        and _parse_finite_env_float("REWARD_API_POLL_INTERVAL_S") is not None
    ):
        return float(base_poll_s)
    return min(max(float(base_poll_s), 1.0), 10.0)


def _get_result_poll_max_s(base_poll_s: float) -> float:
    value = os.environ.get("REWARD_API_RESULT_POLL_MAX_S")
    if value is None or not value.strip():
        return _get_default_result_poll_max_s(base_poll_s)
    try:
        n = float(value)
    except ValueError:
        return _get_default_result_poll_max_s(base_poll_s)
    if not math.isfinite(n):
        return _get_default_result_poll_max_s(base_poll_s)
    return min(max(float(base_poll_s), n), 10.0)


def _get_sample_meta_cache_size() -> int:
    return env_int("REWARD_API_SAMPLE_META_CACHE_SIZE", 2048, lo=0, hi=50000)


def _get_gt_cache_size() -> int:
    return env_int("REWARD_API_GT_CACHE_SIZE", 128, lo=0, hi=10000)


def _get_gt_cache_max_cells() -> int:
    return env_int("REWARD_API_GT_CACHE_MAX_CELLS", 500000, lo=0, hi=10_000_000)


def _get_gt_prepared_max_cells() -> int:
    return env_int("REWARD_API_GT_PREPARED_MAX_CELLS", 500000, lo=1000, hi=10_000_000)


def _get_max_upload_bytes() -> int | None:
    value = os.environ.get("REWARD_API_MAX_UPLOAD_BYTES")
    if value is None or not value.strip():
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    if n <= 0:
        return None
    return n


def _keep_files() -> bool:
    return env_bool("REWARD_API_KEEP_FILES", default=False)


def _get_recalculate_job_root() -> Path:
    return env_path(
        "REWARD_API_RECALC_JOB_ROOT",
        Path(tempfile.gettempdir()) / "async_reward_api_recalculate_jobs",
    )


def _enable_timeout_excel_fallback_kill() -> bool:
    return env_bool("REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL", default=False)


def _get_max_running_jobs() -> int | None:
    value = os.environ.get("REWARD_API_MAX_RUNNING_JOBS")
    if value is None or not value.strip():
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    return max(1, n)
