from __future__ import annotations

import argparse
import json
from pathlib import Path

from .eval import compute_reward
from .platform import Platform, detect_platform, normalize_platform


def _format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _recalc_spreadsheet(
    platform: Platform,
    file_path: Path,
    *,
    excel_pid_file: Path | None = None,
) -> int:
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
    args = parser.parse_args(argv)
    if not args.recalc_only and (not args.gt_file or not args.answer_position):
        parser.error("--gt-file and --answer-position are required unless --recalc-only is set")

    platform = normalize_platform(args.platform) or detect_platform()

    gt_file = Path(args.gt_file) if args.gt_file else None
    proc_file = Path(args.proc_file)
    answer_position = args.answer_position or ""
    excel_pid_file = Path(args.excel_pid_file) if args.excel_pid_file else None

    try:
        status = _recalc_spreadsheet(platform, proc_file, excel_pid_file=excel_pid_file)
        if status != 0 or not proc_file.exists():
            if args.recalc_only:
                print(json.dumps({"ok": False, "msg": "recalc failed"}), flush=True)
            else:
                print(json.dumps({"ok": False, "reward": 0.0, "msg": "recalc failed"}), flush=True)
            return 0

        if args.recalc_only:
            print(json.dumps({"ok": True, "msg": ""}), flush=True)
            return 0

        if gt_file is None:
            print(json.dumps({"ok": False, "reward": 0.0, "msg": "missing gt file"}), flush=True)
            return 0
        reward, msg = compute_reward(gt_file, proc_file, answer_position)
        print(json.dumps({"ok": True, "reward": float(reward), "msg": msg or ""}), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        if args.recalc_only:
            print(
                json.dumps({"ok": False, "msg": f"worker error: {_format_exception(exc)}"}),
                flush=True,
            )
        else:
            print(
                json.dumps({"ok": False, "reward": 0.0, "msg": f"worker error: {_format_exception(exc)}"}),
                flush=True,
            )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
