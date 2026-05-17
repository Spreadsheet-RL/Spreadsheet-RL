from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import openpyxl


def _write_workbook(path: Path, *, value: object) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = value
    wb.save(path)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_json(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _build_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----async_reward_api_{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b"\r\n")

    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    return body, boundary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end API smoke test (submit + poll).")
    parser.add_argument("--platform", choices=["windows"], default="windows")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--wait-s", type=float, default=10.0)
    args = parser.parse_args(argv)

    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="async_reward_api_e2e_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        db_path = tmp_path / "jobs.sqlite3"
        thread_dir = "thread_1"
        sample_dir = output_root / thread_dir
        sample_dir.mkdir(parents=True, exist_ok=True)

        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}, ensure_ascii=False),
            encoding="utf-8",
        )
        _write_workbook(sample_dir / "target.xlsx", value=123)

        env = os.environ.copy()
        env["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        env["REWARD_API_DB_PATH"] = str(db_path)
        env["REWARD_API_KEEP_FILES"] = "1"

        cmd = [
            sys.executable,
            "-m",
            "async_reward_api.cli",
            "--platform",
            args.platform,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(max(1, args.workers)),
            "--log-level",
            "warning",
        ]
        print("starting server:", " ".join(cmd))
        proc = subprocess.Popen(  # noqa: S603,S607 - controlled local command
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for /health.
            deadline = time.time() + 20
            last_err: str | None = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=2)
                    raise RuntimeError(
                        "server exited before becoming healthy\n"
                        f"stdout:\n{stdout}\n"
                        f"stderr:\n{stderr}"
                    )
                try:
                    health = _http_json("GET", f"{base_url}/health")
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    last_err = str(exc)
                    time.sleep(0.2)
                    continue
                print("health:", health)
                break
            else:
                raise RuntimeError(f"server did not become healthy (last error: {last_err})")

            upload_bytes = (sample_dir / "target.xlsx").read_bytes()
            body, boundary = _build_multipart(
                {"thread_dir": thread_dir},
                {
                    "file": (
                        "output.xlsx",
                        upload_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
            submit = _http_json(
                "POST",
                f"{base_url}/reward/submit",
                body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            print("submit:", submit)

            job_id = submit.get("job_id")
            if not job_id:
                raise RuntimeError(f"submit did not return job_id: {submit}")

            result_url = f"{base_url}/reward/result/{job_id}?wait_s={args.wait_s}"
            result = _http_json("GET", result_url)
            print("result:", result)
            if result.get("status") != "done":
                raise RuntimeError(f"job did not finish successfully: {result}")
            if float(result.get("reward") or 0.0) != 1.0:
                raise RuntimeError(f"expected reward=1.0, got: {result}")
            if str(result.get("msg") or ""):
                raise RuntimeError(f"expected empty result msg, got: {result}")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
