from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .eval import compute_reward
from .messages import format_exception as _format_exception
from .messages import public_worker_message as _public_worker_message
from .platform import Platform, detect_platform, normalize_platform


logger = logging.getLogger(__name__)

def _recalc_spreadsheet(
    platform: Platform,
    file_path: Path,
    *,
    excel_pid_file: Path | None = None,
) -> tuple[int, str]:
    if platform is Platform.WINDOWS:
        from .recalc_on_windows import recalc_spreadsheet

        return recalc_spreadsheet(file_path, excel_pid_file=excel_pid_file)
    raise RuntimeError(f"Unsupported platform: {platform}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Async reward worker (recalc + eval)")
    parser.add_argument("--platform", choices=["windows"])
    parser.add_argument("--gt-file")
    parser.add_argument("--proc-file", required=True)
    parser.add_argument("--answer-position")
    parser.add_argument("--recalc-only", action="store_true")
    parser.add_argument("--excel-pid-file")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.recalc_only and (not args.gt_file or not args.answer_position):
        parser.error("--gt-file and --answer-position are required unless --recalc-only is set")

    platform = normalize_platform(args.platform) or detect_platform()

    gt_file = Path(args.gt_file) if args.gt_file else None
    proc_file = Path(args.proc_file)
    answer_position = args.answer_position or ""
    excel_pid_file = Path(args.excel_pid_file) if args.excel_pid_file else None

    try:
        status, recalc_msg = _recalc_spreadsheet(platform, proc_file, excel_pid_file=excel_pid_file)
        if status == 2:
            # Parsed by the API parent process; keep this exact raw stderr sentinel.
            print(f"[worker] fatal recalc failed: {recalc_msg}", file=sys.stderr, flush=True)
            return 2
        if status != 0 or not proc_file.exists():
            logger.warning(f"[worker] recalc failed: {recalc_msg}")
            msg = _public_worker_message(recalc_msg, fallback="recalc failed")
            if args.recalc_only:
                print(json.dumps({"ok": False, "msg": msg}), flush=True)
            else:
                print(json.dumps({"ok": False, "reward": 0.0, "msg": msg}), flush=True)
            return 0

        if args.recalc_only:
            print(json.dumps({"ok": True, "msg": ""}), flush=True)
            return 0

        assert gt_file is not None
        reward, msg = compute_reward(gt_file, proc_file, answer_position)
        public_msg = _public_worker_message(msg, fallback="")
        print(json.dumps({"ok": True, "reward": float(reward), "msg": public_msg}), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        raw_msg = f"worker error: {_format_exception(exc)}"
        logger.warning(f"[worker] {raw_msg}")
        public_msg = _public_worker_message(raw_msg, fallback="worker error")
        if args.recalc_only:
            print(
                json.dumps({"ok": False, "msg": public_msg}),
                flush=True,
            )
        else:
            print(
                json.dumps({"ok": False, "reward": 0.0, "msg": public_msg}),
                flush=True,
            )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
