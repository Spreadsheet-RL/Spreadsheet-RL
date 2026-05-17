from __future__ import annotations

import os
import sys
from enum import Enum


_ALLOW_UNSUPPORTED_HOST_ENV = "REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"


class Platform(str, Enum):
    WINDOWS = "windows"


def normalize_platform(value: str | None) -> Platform | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"windows", "win", "win32"}:
        return Platform.WINDOWS
    return None


def is_windows_host() -> bool:
    return sys.platform.startswith(("win", "cygwin", "msys"))


def allow_unsupported_host_for_tests() -> bool:
    value = os.environ.get(_ALLOW_UNSUPPORTED_HOST_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def detect_platform() -> Platform:
    if not is_windows_host() and not allow_unsupported_host_for_tests():
        raise RuntimeError(
            "Async reward API is Windows-only because Excel recalculation uses COM automation."
        )

    from_env = normalize_platform(os.environ.get("REWARD_API_PLATFORM"))
    if from_env is not None:
        return from_env
    if is_windows_host():
        return Platform.WINDOWS
    raise RuntimeError(
        f"Set {_ALLOW_UNSUPPORTED_HOST_ENV}=1 only for non-production contract tests."
    )
