from __future__ import annotations

import argparse
import os

import uvicorn

from .platform import allow_unsupported_host_for_tests, is_windows_host, normalize_platform


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Async Spreadsheet Reward API server")
    parser.add_argument("--platform", choices=["windows"], default="windows")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--instance-per-worker",
        type=int,
        default=None,
        help="Windows-only: number of persistent Excel instances per API worker (0 disables).",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    platform = normalize_platform(args.platform)
    if platform is None:
        raise SystemExit(f"Unsupported --platform: {args.platform}")
    if not is_windows_host() and not allow_unsupported_host_for_tests():
        raise SystemExit(
            "async-reward-api is Windows-only. "
            "Set REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS=1 only for local contract tests."
        )

    os.environ["REWARD_API_PLATFORM"] = platform.value
    if args.instance_per_worker is not None:
        os.environ["REWARD_API_INSTANCE_PER_WORKER"] = str(max(0, int(args.instance_per_worker)))

    uvicorn.run(
        "async_reward_api.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
