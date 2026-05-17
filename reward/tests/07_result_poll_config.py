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

    from async_reward_api.main import _get_result_poll_interval_s, _get_result_poll_max_s

    for name in (
        "REWARD_API_POLL_INTERVAL_S",
        "REWARD_API_RESULT_POLL_INTERVAL_S",
        "REWARD_API_RESULT_POLL_MAX_S",
    ):
        _set_env(name, None)

    interval = _get_result_poll_interval_s()
    _assert_equal("default interval", interval, 0.2)
    _assert_equal("default max", _get_result_poll_max_s(interval), 1.0)

    _set_env("REWARD_API_POLL_INTERVAL_S", "0.7")
    interval = _get_result_poll_interval_s()
    _assert_equal("legacy interval fallback", interval, 0.7)
    _assert_equal("legacy max fallback", _get_result_poll_max_s(interval), 0.7)

    _set_env("REWARD_API_RESULT_POLL_MAX_S", "1.5")
    _assert_equal("explicit result max", _get_result_poll_max_s(interval), 1.5)

    _set_env("REWARD_API_RESULT_POLL_INTERVAL_S", "0.1")
    _set_env("REWARD_API_RESULT_POLL_MAX_S", None)
    interval = _get_result_poll_interval_s()
    _assert_equal("result interval override", interval, 0.1)
    _assert_equal("result max default with result interval", _get_result_poll_max_s(interval), 1.0)

    print("OK: result poll config looks good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
