from __future__ import annotations

import os


def _assert_equal(label: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{label}:\n  got={got!r}\n  expected={expected!r}")


def _set_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
        return
    os.environ[name] = value


def main() -> int:
    os.environ["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
    os.environ["REWARD_API_PLATFORM"] = "windows"

    from async_reward_api.config import (
        _enable_timeout_excel_fallback_kill,
        _get_cleanup_leader_lease_s,
        _get_cleanup_retry_after_s,
        _get_cleanup_retry_batch_share,
        _get_cleanup_retry_max_s,
        _get_gt_cache_max_cells,
        _get_gt_prepared_max_cells,
        _get_idle_poll_max_s,
        _get_job_ttl_s,
        _get_poll_interval_s,
        _get_result_poll_interval_s,
        _get_result_poll_max_s,
        _get_sqlite_executor_workers,
        _get_stale_sweep_leader_lease_s,
        _get_windows_excel_diagnostics_dir,
        _get_worker_timeout_s,
        _keep_files,
    )

    for name in (
        "REWARD_API_POLL_INTERVAL_S",
        "REWARD_API_IDLE_POLL_MAX_S",
        "REWARD_API_RESULT_POLL_INTERVAL_S",
        "REWARD_API_RESULT_POLL_MAX_S",
        "REWARD_API_SQLITE_EXECUTOR_WORKERS",
        "REWARD_API_GT_CACHE_MAX_CELLS",
        "REWARD_API_GT_PREPARED_MAX_CELLS",
        "REWARD_API_KEEP_FILES",
        "REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL",
        "REWARD_API_WORKER_TIMEOUT_S",
        "REWARD_API_JOB_TTL_S",
        "REWARD_API_CLEANUP_LEADER_LEASE_S",
        "REWARD_API_STALE_SWEEP_LEADER_LEASE_S",
        "REWARD_API_CLEANUP_RETRY_AFTER_S",
        "REWARD_API_CLEANUP_RETRY_MAX_S",
        "REWARD_API_CLEANUP_RETRY_BATCH_SHARE",
        "REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR",
    ):
        _set_env(name, None)

    interval = _get_result_poll_interval_s()
    _assert_equal("default interval", interval, 0.2)
    _assert_equal("default max", _get_result_poll_max_s(interval), 1.0)
    _assert_equal("default idle max", _get_idle_poll_max_s(interval), 0.5)
    _assert_equal("default SQLite executor workers", _get_sqlite_executor_workers(), 2)
    _assert_equal("default GT cache max cells", _get_gt_cache_max_cells(), 500000)
    _assert_equal("default GT prepared max cells", _get_gt_prepared_max_cells(), 500000)

    for value, expected in (
        ("0", 1),
        ("1", 1),
        ("4", 4),
        ("9", 8),
        ("not-an-integer", 2),
    ):
        _set_env("REWARD_API_SQLITE_EXECUTOR_WORKERS", value)
        _assert_equal(f"SQLite executor workers {value!r}", _get_sqlite_executor_workers(), expected)
    _set_env("REWARD_API_SQLITE_EXECUTOR_WORKERS", None)

    for value, expected in (
        ("-1", 0),
        ("0", 0),
        ("750000", 750000),
        ("10000001", 10_000_000),
        ("not-an-integer", 500000),
    ):
        _set_env("REWARD_API_GT_CACHE_MAX_CELLS", value)
        _assert_equal(f"GT cache max cells {value!r}", _get_gt_cache_max_cells(), expected)
    _set_env("REWARD_API_GT_CACHE_MAX_CELLS", None)

    for value, expected in (
        ("-1", 1000),
        ("0", 1000),
        ("750000", 750000),
        ("10000001", 10_000_000),
        ("not-an-integer", 500000),
    ):
        _set_env("REWARD_API_GT_PREPARED_MAX_CELLS", value)
        _assert_equal(f"GT prepared max cells {value!r}", _get_gt_prepared_max_cells(), expected)
    _set_env("REWARD_API_GT_PREPARED_MAX_CELLS", None)

    for value, expected in (
        ("0.1", 0.2),
        ("1.25", 1.25),
        ("99", 30.0),
        ("not-a-number", 0.5),
        ("nan", 0.5),
        ("inf", 0.5),
    ):
        _set_env("REWARD_API_IDLE_POLL_MAX_S", value)
        _assert_equal(f"idle poll max {value!r}", _get_idle_poll_max_s(interval), expected)
    _set_env("REWARD_API_IDLE_POLL_MAX_S", "not-a-number")
    _assert_equal("invalid idle max follows slower base poll", _get_idle_poll_max_s(0.7), 0.7)
    _set_env("REWARD_API_IDLE_POLL_MAX_S", None)
    _assert_equal("idle max follows slower base poll", _get_idle_poll_max_s(0.7), 0.7)

    _set_env("REWARD_API_POLL_INTERVAL_S", "0.7")
    interval = _get_result_poll_interval_s()
    _assert_equal("legacy interval fallback", interval, 0.7)
    _assert_equal("legacy max fallback", _get_result_poll_max_s(interval), 0.7)
    for value in ("not-a-number", "nan", "inf", "-inf"):
        _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", value)
        interval = _get_result_poll_interval_s()
        _assert_equal(f"invalid result interval legacy fallback {value!r}", interval, 0.7)
        _assert_equal(f"invalid result interval legacy max fallback {value!r}", _get_result_poll_max_s(interval), 0.7)
    _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", None)

    _set_env("REWARD_API_RESULT_POLL_MAX_S", "1.5")
    _assert_equal("explicit result max", _get_result_poll_max_s(interval), 1.5)
    _set_env("REWARD_API_RESULT_POLL_MAX_S", "not-a-number")
    _assert_equal("invalid result max legacy default", _get_result_poll_max_s(interval), 0.7)
    for value in ("nan", "inf", "-inf"):
        _set_env("REWARD_API_RESULT_POLL_MAX_S", value)
        _assert_equal(f"non-finite result max legacy default {value!r}", _get_result_poll_max_s(interval), 0.7)
    _set_env("REWARD_API_POLL_INTERVAL_S", None)
    interval = _get_result_poll_interval_s()
    _set_env("REWARD_API_RESULT_POLL_MAX_S", "not-a-number")
    _assert_equal("invalid result max default", _get_result_poll_max_s(interval), 1.0)
    for value in ("nan", "inf", "-inf"):
        _set_env("REWARD_API_RESULT_POLL_MAX_S", value)
        _assert_equal(f"non-finite result max default {value!r}", _get_result_poll_max_s(interval), 1.0)

    _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", "0.1")
    _set_env("REWARD_API_RESULT_POLL_MAX_S", None)
    interval = _get_result_poll_interval_s()
    _assert_equal("result interval override", interval, 0.1)
    _assert_equal("result max default with result interval", _get_result_poll_max_s(interval), 1.0)
    _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", "2.5")
    interval = _get_result_poll_interval_s()
    _assert_equal("result interval valid override", interval, 2.5)
    _assert_equal("result max default follows valid result interval", _get_result_poll_max_s(interval), 2.5)

    for value in ("not-a-number", "nan", "inf", "-inf"):
        _set_env("REWARD_API_POLL_INTERVAL_S", value)
        _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", None)
        interval = _get_result_poll_interval_s()
        _assert_equal(f"invalid poll interval fallback {value!r}", _get_poll_interval_s(), 0.2)
        _assert_equal(f"invalid legacy result interval fallback {value!r}", interval, 0.2)
        _assert_equal(f"invalid legacy result max default {value!r}", _get_result_poll_max_s(interval), 1.0)
    _set_env("REWARD_API_POLL_INTERVAL_S", None)
    _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", None)

    for value in ("", "0", "false", "False", "FALSE", "no", "No", "off", "OFF"):
        _set_env("REWARD_API_KEEP_FILES", value)
        _set_env("REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL", value)
        _assert_equal(f"keep files false token {value!r}", _keep_files(), False)
        _assert_equal(
            f"fallback kill false token {value!r}",
            _enable_timeout_excel_fallback_kill(),
            False,
        )

    for value in ("1", "true", "True", "TRUE", "yes", "Y", "on", "ON"):
        _set_env("REWARD_API_KEEP_FILES", value)
        _set_env("REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL", value)
        _assert_equal(f"keep files true token {value!r}", _keep_files(), True)
        _assert_equal(
            f"fallback kill true token {value!r}",
            _enable_timeout_excel_fallback_kill(),
            True,
        )

    for value in ("nan", "inf", "-inf", "0", "-1"):
        _set_env("REWARD_API_WORKER_TIMEOUT_S", value)
        _assert_equal(f"worker timeout fallback {value!r}", _get_worker_timeout_s(), 240.0)

    _set_env("REWARD_API_WORKER_TIMEOUT_S", "12.5")
    _assert_equal("finite worker timeout", _get_worker_timeout_s(), 12.5)

    _set_env("REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR", None)
    _assert_equal("diagnostics cleanup default disabled", _get_windows_excel_diagnostics_dir(), None)

    for value in ("nan", "inf", "-inf"):
        _set_env("REWARD_API_JOB_TTL_S", value)
        _assert_equal(f"job ttl fallback {value!r}", _get_job_ttl_s(), 3600.0)

    for value in ("nan", "inf", "-inf"):
        _set_env("REWARD_API_CLEANUP_LEADER_LEASE_S", value)
        _set_env("REWARD_API_STALE_SWEEP_LEADER_LEASE_S", value)
        _set_env("REWARD_API_CLEANUP_RETRY_AFTER_S", value)
        _set_env("REWARD_API_CLEANUP_RETRY_MAX_S", value)
        _set_env("REWARD_API_CLEANUP_RETRY_BATCH_SHARE", value)
        _assert_equal(f"cleanup lease fallback {value!r}", _get_cleanup_leader_lease_s(), 900.0)
        _assert_equal(f"stale sweep lease fallback {value!r}", _get_stale_sweep_leader_lease_s(), 30.0)
        _assert_equal(f"cleanup retry after fallback {value!r}", _get_cleanup_retry_after_s(), 300.0)
        _assert_equal(f"cleanup retry max fallback {value!r}", _get_cleanup_retry_max_s(), 3600.0)
        _assert_equal(f"cleanup retry share fallback {value!r}", _get_cleanup_retry_batch_share(), 0.25)

    print("OK: result poll config looks good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
