from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import io
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import openpyxl


def _write_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = 123
    wb.save(path)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_bytes(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _http_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> tuple[int, dict[str, object]]:
    status, data = _http_bytes(method, url, body=body, headers=headers, timeout_s=timeout_s)
    return status, json.loads(data.decode("utf-8"))


def _build_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----async_reward_api_{uuid.uuid4().hex}"
    content = file_path.read_bytes()
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b"\r\n")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode(),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def _looks_like_xlsx(content: bytes) -> bool:
    if not content.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return "[Content_Types].xml" in set(zf.namelist())
    except Exception:
        return False


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="async_reward_api_contract_") as tmp:
        tmp_path = Path(tmp)
        workbook = tmp_path / "workbook.xlsx"
        _write_workbook(workbook)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        _write_workbook(sample_dir / "target.xlsx")

        env = os.environ.copy()
        env["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
        env["REWARD_API_PLATFORM"] = "windows"
        env["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        env["REWARD_API_DB_PATH"] = str(tmp_path / "jobs.sqlite3")
        env["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        env["REWARD_API_INSTANCE_PER_WORKER"] = "0"
        env["REWARD_API_WORKER_TIMEOUT_S"] = "1"

        cmd = [
            sys.executable,
            "-m",
            "async_reward_api.cli",
            "--platform",
            "windows",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--log-level",
            "warning",
        ]
        proc = subprocess.Popen(  # noqa: S603,S607 - controlled local command
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 20
            health: dict[str, object] | None = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=2)
                    raise AssertionError(
                        "server exited before becoming healthy\n"
                        f"stdout:\n{stdout}\n"
                        f"stderr:\n{stderr}"
                    )
                try:
                    status, payload = _http_json("GET", f"{base_url}/health", timeout_s=2)
                except (OSError, ValueError, TimeoutError):
                    time.sleep(0.2)
                    continue
                if status == 200:
                    health = payload
                    break
                time.sleep(0.2)
            _assert(health is not None, "server did not become healthy")
            _assert("db_path" not in health, "/health must not expose db_path")
            _assert("worker_id" not in health, "/health must not expose worker_id")
            _assert(isinstance(health.get("instance_id"), str), "/health must include instance_id")
            _assert(isinstance(health.get("db_fingerprint"), str), "/health must include db_fingerprint")
            _assert(health.get("ready") is True, "/health must report readiness")
            _assert(health.get("background_tasks_healthy") is True, "/health must report task health")
            background_tasks = health.get("background_tasks")
            _assert(isinstance(background_tasks, dict), "/health must include background task status")
            _assert(
                background_tasks.get("worker_loop", {}).get("state") == "running",
                "worker loop should be running",
            )
            _assert(
                background_tasks.get("cleanup_loop", {}).get("state") == "running",
                "cleanup loop should be running",
            )

            excel_pool = health.get("excel_pool")
            _assert(isinstance(excel_pool, dict), "/health must include excel_pool status")
            _assert(excel_pool.get("enabled") is False, "contract test disables the Excel pool")
            _assert(excel_pool.get("mode") == "per_job", "disabled pool should report per_job mode")
            _assert(excel_pool.get("alive_instances") == 0, "disabled pool should report no live workers")
            _assert(health.get("excel_pool_healthy") is True, "intentionally disabled pool is healthy")

            body, boundary = _build_multipart({}, workbook)
            submit_status, submit = _http_json(
                "POST",
                f"{base_url}/recalculate/submit",
                body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            _assert(submit_status == 200, f"recalculate submit failed: {submit_status} {submit}")
            job_id = submit.get("job_id")
            _assert(isinstance(job_id, str) and bool(job_id), "recalculate submit did not return job_id")

            recalc_status, recalc_payload = _http_json(
                "GET",
                f"{base_url}/recalculate/status/{job_id}",
            )
            _assert(recalc_status == 200, f"recalculate status failed: {recalc_status} {recalc_payload}")

            result_status, result_content = _http_bytes(
                "GET",
                f"{base_url}/recalculate/result/{job_id}?wait_s=0.001",
            )
            _assert(result_status == 200, f"recalculate result failed: {result_status}")
            if not _looks_like_xlsx(result_content):
                recalc_result = json.loads(result_content.decode("utf-8"))
                _assert(
                    recalc_result.get("status") in {"queued", "running", "error"},
                    f"unexpected recalculate result payload: {recalc_result}",
                )

            reward_status, _ = _http_json("GET", f"{base_url}/reward/status/{job_id}")
            _assert(reward_status == 404, "reward status endpoint must reject recalculate jobs")

            reward_result, _ = _http_json("GET", f"{base_url}/reward/result/{job_id}")
            _assert(reward_result == 404, "reward result endpoint must reject recalculate jobs")

            reward_body, reward_boundary = _build_multipart({"thread_dir": "thread_1"}, workbook)
            reward_submit_status, reward_submit = _http_json(
                "POST",
                f"{base_url}/reward/submit",
                body=reward_body,
                headers={"Content-Type": f"multipart/form-data; boundary={reward_boundary}"},
            )
            _assert(
                reward_submit_status == 200,
                f"reward submit failed: {reward_submit_status} {reward_submit}",
            )
            reward_job_id = reward_submit.get("job_id")
            _assert(
                isinstance(reward_job_id, str) and bool(reward_job_id),
                "reward submit did not return job_id",
            )
            reward_result_status, reward_payload = _http_json(
                "GET",
                f"{base_url}/reward/result/{reward_job_id}?wait_s=5",
            )
            _assert(reward_result_status == 200, f"reward result failed: {reward_result_status}")
            if os.name != "nt":
                _assert(
                    reward_payload.get("status") == "error",
                    f"reward infrastructure failure should be an error: {reward_payload}",
                )

            wrong_recalc_status, _ = _http_json(
                "GET",
                f"{base_url}/recalculate/status/{reward_job_id}",
            )
            _assert(
                wrong_recalc_status == 404,
                "recalculate status endpoint must reject reward jobs",
            )

            wrong_recalc_result, _ = _http_json(
                "GET",
                f"{base_url}/recalculate/result/{reward_job_id}",
            )
            _assert(
                wrong_recalc_result == 404,
                "recalculate result endpoint must reject reward jobs",
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    print("OK: API contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
