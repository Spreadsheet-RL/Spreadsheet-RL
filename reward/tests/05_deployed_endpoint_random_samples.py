from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import mimetypes
import os
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


_PRODUCTION_HOST = os.environ.get("SPREADSHEET_RL_PRODUCTION_HOST", "").strip().lower()
_DEFAULT_RECALC_URL = "http://127.0.0.1:5000/recalculate"
_MAX_WORKERS_CAP = 8


class HttpResponseError(RuntimeError):
    def __init__(self, status: int, reason: str, body: str) -> None:
        super().__init__(f"HTTP {status} {reason}: {body}")
        self.status = int(status)
        self.reason = str(reason)
        self.body = str(body)


def _http_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        text = payload.decode("utf-8", errors="replace")
        raise HttpResponseError(exc.code, exc.reason, text) from exc
    return json.loads(data.decode("utf-8"))


def _add_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
    )


def _url_join(base: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return urllib.parse.urljoin(base, path)


def _is_production_url(url: str) -> bool:
    if not _PRODUCTION_HOST:
        return False
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    return host.rstrip(".").lower() == _PRODUCTION_HOST


def _discover_samples(dataset_root: Path) -> list[tuple[str, Path]]:
    samples: list[tuple[str, Path]] = []
    for instr_path in dataset_root.rglob("instruction.json"):
        sample_dir = instr_path.parent
        target_path = sample_dir / "target.xlsx"
        if not target_path.exists():
            continue
        try:
            thread_dir = sample_dir.relative_to(dataset_root).as_posix()
        except ValueError:
            continue
        samples.append((thread_dir, target_path))
    return samples


@dataclass(frozen=True)
class SampleResult:
    thread_dir: str
    job_id: str | None
    status: str
    reward: float | None
    submit_s: float
    total_s: float
    msg: str
    recalc_s: float | None = None
    recalc_bytes: int | None = None
    recalc_msg: str = ""


def _normalize_submit_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url

    scheme = parts.scheme or "https"
    netloc = parts.netloc
    path = parts.path or ""
    query = parts.query
    fragment = parts.fragment

    if not netloc and parts.path and "://" not in url:
        try:
            parts = urllib.parse.urlsplit(f"{scheme}://{url}")
            netloc = parts.netloc
            path = parts.path or ""
            query = parts.query
            fragment = parts.fragment
        except Exception:
            return url

    normalized_path = path.rstrip("/")
    if not normalized_path or normalized_path == "/":
        normalized_path = "/reward/submit"
    elif normalized_path == "/submit":
        normalized_path = "/reward/submit"
    elif normalized_path == "/reward":
        normalized_path = "/reward/submit"

    return urllib.parse.urlunsplit((scheme, netloc, normalized_path, query, fragment))


def _normalize_recalc_url(url: str, *, submit_url: str) -> str:
    url = (url or "").strip()
    if not url:
        parts = urllib.parse.urlsplit(submit_url)
        if parts.scheme and parts.netloc:
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/recalculate", "", ""))
        return _DEFAULT_RECALC_URL

    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url

    scheme = parts.scheme or "https"
    netloc = parts.netloc
    path = parts.path or ""
    query = parts.query
    fragment = parts.fragment

    if not netloc and parts.path and "://" not in url:
        try:
            parts = urllib.parse.urlsplit(f"{scheme}://{url}")
            netloc = parts.netloc
            path = parts.path or ""
            query = parts.query
            fragment = parts.fragment
        except Exception:
            return url

    normalized_path = path.rstrip("/")
    if not normalized_path or normalized_path == "/":
        normalized_path = "/recalculate"

    return urllib.parse.urlunsplit((scheme, netloc, normalized_path, query, fragment))


def _http_bytes(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        text = payload.decode("utf-8", errors="replace")
        raise HttpResponseError(exc.code, exc.reason, text) from exc


def _looks_like_xlsx(content: bytes) -> bool:
    if not content or len(content) < 4 or not content.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return "[Content_Types].xml" in set(zf.namelist())
    except Exception:
        return False


def _post_recalculate(
    *,
    recalc_url: str,
    file_path: Path,
    timeout_s: float,
) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    filename = file_path.name
    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    file_bytes = file_path.read_bytes()

    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    lines: list[bytes] = []

    lines.append(f"--{boundary}".encode())
    lines.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode())
    lines.append(f"Content-Type: {mime_type}".encode())
    lines.append(b"")
    lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = crlf.join(lines)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    deadline = time.monotonic() + float(timeout_s)
    submit_retry_interval_s = _get_submit_retry_interval_s()
    submit_payload: dict | None = None
    while True:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise RuntimeError("timeout during recalculate submit")
        try:
            content = _http_bytes(
                "POST",
                recalc_url,
                body=body,
                headers=headers,
                timeout_s=min(30.0, remaining_s),
            )
        except HttpResponseError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue

        if _looks_like_xlsx(content):
            return content, time.perf_counter() - t0

        try:
            submit_payload = json.loads(content.decode("utf-8", errors="replace"))
        except ValueError:
            raise RuntimeError("recalculate submit returned invalid JSON") from None

        if submit_payload.get("status") == "busy":
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue

        job_id = submit_payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue
        break

    result_path = submit_payload.get("result_path") if submit_payload else None
    if isinstance(result_path, str) and result_path:
        result_url = _url_join(recalc_url, result_path)
    else:
        result_url = _url_join(recalc_url, f"/recalculate/result/{job_id}")

    backoff_s = 0.5
    while True:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise RuntimeError("timeout during recalculate poll")

        wait_s = min(10.0, max(0.0, remaining_s))
        poll_url = _add_query_params(result_url, {"wait_s": f"{wait_s:.3f}"})
        try:
            polled = _http_bytes(
                "GET",
                poll_url,
                timeout_s=min(remaining_s, max(5.0, wait_s + 15.0)),
            )
        except HttpResponseError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2, 5.0)
            continue
        except (urllib.error.URLError, OSError, TimeoutError):
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2, 5.0)
            continue

        if _looks_like_xlsx(polled):
            return polled, time.perf_counter() - t0

        try:
            poll_payload = json.loads(polled.decode("utf-8", errors="replace"))
        except ValueError:
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2, 5.0)
            continue

        status = poll_payload.get("status")
        if status == "error":
            msg = str(poll_payload.get("msg") or "")
            raise RuntimeError(msg or "recalculate failed")
        if status == "done":
            raise RuntimeError("recalculate poll returned JSON done without a file")


def _get_submit_retry_interval_s() -> float:
    value = os.environ.get("SPREADSHEET_RL_REWARD_SUBMIT_RETRY_INTERVAL_S", "20")
    try:
        retry_s = float(value)
        return max(0.1, retry_s)
    except ValueError:
        return 20.0


def _run_one(
    *,
    submit_url: str,
    recalc_url: str,
    thread_dir: str,
    target_path: Path,
    wait_s: float,
    timeout_s: float,
) -> SampleResult:
    t0 = time.perf_counter()
    filename = target_path.name
    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    file_bytes = target_path.read_bytes()

    recalc_s: float | None = None
    recalc_bytes: int | None = None
    recalc_msg = ""
    if recalc_url:
        recalc_path = target_path.parent / "output.xlsx"
        if not recalc_path.exists():
            recalc_path = target_path
        recalc_content, recalc_s = _post_recalculate(
            recalc_url=recalc_url,
            file_path=recalc_path,
            timeout_s=min(180.0, float(timeout_s)),
        )
        recalc_bytes = len(recalc_content)
        recalc_msg = f"ok ({recalc_path.name})"

    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    lines: list[bytes] = []

    lines.append(f"--{boundary}".encode())
    lines.append(b'Content-Disposition: form-data; name="thread_dir"')
    lines.append(b"")
    lines.append(thread_dir.encode())

    lines.append(f"--{boundary}".encode())
    lines.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode()
    )
    lines.append(f"Content-Type: {mime_type}".encode())
    lines.append(b"")
    lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = crlf.join(lines)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    submit_timeout_s = min(30.0, float(timeout_s))
    submit_retry_interval_s = _get_submit_retry_interval_s()
    deadline = time.monotonic() + float(timeout_s)

    submit_payload: dict | None = None
    while True:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return SampleResult(
                thread_dir=thread_dir,
                job_id=None,
                status="timeout",
                reward=None,
                submit_s=0.0,
                total_s=time.perf_counter() - t0,
                msg="timeout during submit",
                recalc_s=recalc_s,
                recalc_bytes=recalc_bytes,
                recalc_msg=recalc_msg,
            )
        try:
            submit_payload = _http_json(
                "POST",
                submit_url,
                body=body,
                headers=headers,
                timeout_s=min(submit_timeout_s, remaining_s),
            )
        except HttpResponseError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue

        if "reward" in submit_payload:
            reward = float(submit_payload.get("reward") or 0.0)
            msg = str(submit_payload.get("msg") or "")
            return SampleResult(
                thread_dir=thread_dir,
                job_id=None,
                status=str(submit_payload.get("status") or "done"),
                reward=reward,
                submit_s=time.perf_counter() - t0,
                total_s=time.perf_counter() - t0,
                msg=msg,
                recalc_s=recalc_s,
                recalc_bytes=recalc_bytes,
                recalc_msg=recalc_msg,
            )

        if submit_payload.get("status") == "busy":
            time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
            continue

        job_id = submit_payload.get("job_id")
        if isinstance(job_id, str) and job_id:
            break

        time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))

    t_submit = time.perf_counter()

    job_id_str = str(job_id)
    result_path = submit_payload.get("result_path") if isinstance(submit_payload, dict) else None
    if isinstance(result_path, str) and result_path:
        result_url = _url_join(submit_url, result_path)
    else:
        result_url = _url_join(submit_url, f"/reward/result/{job_id_str}")

    backoff_s = 0.5
    while True:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return SampleResult(
                thread_dir=thread_dir,
                job_id=job_id_str,
                status="timeout",
                reward=None,
                submit_s=t_submit - t0,
                total_s=time.perf_counter() - t0,
                msg="timeout during poll",
                recalc_s=recalc_s,
                recalc_bytes=recalc_bytes,
                recalc_msg=recalc_msg,
            )

        wait = min(float(wait_s), max(0.0, remaining_s))
        poll_url = _add_query_params(result_url, {"wait_s": f"{wait:.3f}"})
        poll_timeout_s = min(remaining_s, max(5.0, wait + 15.0))
        try:
            result = _http_json("GET", poll_url, timeout_s=poll_timeout_s)
        except HttpResponseError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2.0, 5.0)
            continue
        except Exception:
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2.0, 5.0)
            continue

        status = str(result.get("status") or "")
        if status in {"done", "error"}:
            reward = float(result.get("reward") or 0.0)
            msg = str(result.get("msg") or "")
            return SampleResult(
                thread_dir=thread_dir,
                job_id=job_id_str,
                status=status,
                reward=reward,
                submit_s=t_submit - t0,
                total_s=time.perf_counter() - t0,
                msg=msg,
                recalc_s=recalc_s,
                recalc_bytes=recalc_bytes,
                recalc_msg=recalc_msg,
            )

        if float(wait_s) <= 0:
            sleep_s = min(backoff_s, max(0.0, remaining_s))
            time.sleep(sleep_s)
            backoff_s = min(backoff_s * 2.0, 5.0)


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    q = min(max(q, 0.0), 1.0)
    idx = int(round(q * (len(sorted_values) - 1)))
    return float(sorted_values[idx])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark deployed reward API using random local dataset samples (submit + poll)."
    )
    parser.add_argument(
        "--dataset-root",
        default=os.environ.get("SPREADSHEET_RL_DATA_ROOT", ""),
        help="Local dataset root containing <thread_dir>/instruction.json + target.xlsx",
    )
    parser.add_argument(
        "--submit-url",
        default=os.environ.get("SPREADSHEET_RL_REWARD_URL", ""),
        help="Deployed submit endpoint (expects multipart fields: thread_dir, file)",
    )
    parser.add_argument(
        "--recalc-n",
        type=int,
        default=0,
        help="If >0, call /recalculate for the first N picked samples (smoke test).",
    )
    parser.add_argument(
        "--recalc-url",
        default=os.environ.get("SPREADSHEET_RL_RECALC_URL", ""),
        help="Deployed /recalculate endpoint (expects multipart field: file).",
    )
    parser.add_argument("--n", type=int, default=16)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("SPREADSHEET_RL_REWARD_MAX_WORKERS", "4")),
        help=f"Max client-side parallel requests; capped at {_MAX_WORKERS_CAP}.",
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="Required when --submit-url points at SPREADSHEET_RL_PRODUCTION_HOST.",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--wait-s", type=float, default=25.0)
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if not args.dataset_root:
        raise SystemExit("--dataset-root is required, or set SPREADSHEET_RL_DATA_ROOT")
    dataset_root = Path(args.dataset_root).expanduser()
    if not dataset_root.exists():
        raise SystemExit(f"dataset root does not exist: {dataset_root}")

    candidates = _discover_samples(dataset_root)
    if not candidates:
        raise SystemExit(f"no samples found under: {dataset_root}")

    n = max(1, int(args.n))
    if n > len(candidates):
        raise SystemExit(f"requested n={n} but only found {len(candidates)} samples")

    seed = int(args.seed) if args.seed is not None else int(time.time())
    rnd = random.Random(seed)
    picked = rnd.sample(candidates, k=n)

    raw_submit_url = str(args.submit_url or "").strip()
    if not raw_submit_url:
        raise SystemExit("--submit-url is required, or set SPREADSHEET_RL_REWARD_URL")

    submit_url = _normalize_submit_url(raw_submit_url)
    if _is_production_url(submit_url) and not bool(args.confirm_production):
        raise SystemExit(
            f"{_PRODUCTION_HOST} requires --confirm-production to avoid accidental load"
        )
    max_workers_requested = max(1, int(args.max_workers))
    max_workers = min(n, max_workers_requested, _MAX_WORKERS_CAP)
    recalc_n = max(0, int(args.recalc_n))
    recalc_url = ""
    if recalc_n > 0:
        recalc_url = _normalize_recalc_url(str(args.recalc_url), submit_url=submit_url)
    print(f"submit_url={submit_url}")
    if recalc_n > 0:
        print(f"recalc_url={recalc_url} recalc_n={recalc_n}")
    print(f"dataset_root={dataset_root}")
    print(
        f"seed={seed} n={n} max_workers={max_workers} "
        f"wait_s={args.wait_s} timeout_s={args.timeout_s}"
    )
    if max_workers < max_workers_requested:
        print(f"max_workers capped from {max_workers_requested} to {max_workers}")

    results: list[SampleResult | None] = [None] * n
    meta: dict[concurrent.futures.Future[SampleResult], tuple[int, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, (thread_dir, target_path) in enumerate(picked, start=1):
            print(f"[{i:02d}/{n}] queued {thread_dir}")
            fut = executor.submit(
                _run_one,
                submit_url=submit_url,
                recalc_url=recalc_url if i <= recalc_n else "",
                thread_dir=thread_dir,
                target_path=target_path,
                wait_s=float(args.wait_s),
                timeout_s=float(args.timeout_s),
            )
            meta[fut] = (i, thread_dir)

        for fut in concurrent.futures.as_completed(meta):
            i, thread_dir = meta[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[{i:02d}/{n}] {thread_dir}")
                print(f"  error: {type(exc).__name__}: {exc}")
                results[i - 1] = SampleResult(
                    thread_dir=thread_dir,
                    job_id=None,
                    status="client_error",
                    reward=None,
                    submit_s=0.0,
                    total_s=0.0,
                    msg=str(exc),
                )
                continue

            print(f"[{i:02d}/{n}] {thread_dir}")
            print(f"  status={res.status} reward={res.reward} submit_s={res.submit_s:.3f} total_s={res.total_s:.3f}")
            if res.recalc_s is not None:
                print(f"  recalc_s={res.recalc_s:.3f} recalc_bytes={res.recalc_bytes} recalc_msg={res.recalc_msg}")
            results[i - 1] = res

    finalized: list[SampleResult] = [r for r in results if r is not None]

    done = [r for r in finalized if r.status == "done"]
    ok = [r for r in done if r.reward == 1.0]
    errors = [r for r in finalized if r.status not in {"done"}]

    total_times = sorted([r.total_s for r in finalized if r.total_s > 0])
    submit_times = sorted([r.submit_s for r in finalized if r.submit_s > 0])
    recalc_times = sorted([r.recalc_s for r in finalized if r.recalc_s is not None and r.recalc_s > 0])
    recalc_bytes = sorted([r.recalc_bytes for r in finalized if r.recalc_bytes is not None and r.recalc_bytes > 0])

    def _fmt(v: float) -> str:
        return f"{v:.3f}s"

    def _fmt_bytes(v: float) -> str:
        if v < 1024:
            return f"{v:.0f}B"
        if v < 1024 * 1024:
            return f"{v / 1024:.1f}KiB"
        return f"{v / (1024 * 1024):.1f}MiB"

    print("\nSummary:")
    print(
        f"  done={len(done)}/{len(finalized)} ok_reward_1={len(ok)}/{len(finalized)} errors={len(errors)}"
    )
    if total_times:
        print(
            "  total_s:"
            f" mean={_fmt(statistics.mean(total_times))}"
            f" median={_fmt(statistics.median(total_times))}"
            f" p90={_fmt(_quantile(total_times, 0.90))}"
            f" p99={_fmt(_quantile(total_times, 0.99))}"
        )
    if submit_times:
        print(
            "  submit_s:"
            f" mean={_fmt(statistics.mean(submit_times))}"
            f" median={_fmt(statistics.median(submit_times))}"
            f" p90={_fmt(_quantile(submit_times, 0.90))}"
        )
    if recalc_times:
        print(
            "  recalc_s:"
            f" n={len(recalc_times)}"
            f" mean={_fmt(statistics.mean(recalc_times))}"
            f" median={_fmt(statistics.median(recalc_times))}"
            f" p90={_fmt(_quantile(recalc_times, 0.90))}"
            f" p99={_fmt(_quantile(recalc_times, 0.99))}"
        )
    if recalc_bytes:
        print(
            "  recalc_bytes:"
            f" n={len(recalc_bytes)}"
            f" mean={_fmt_bytes(statistics.mean(recalc_bytes))}"
            f" median={_fmt_bytes(statistics.median(recalc_bytes))}"
            f" p90={_fmt_bytes(_quantile(recalc_bytes, 0.90))}"
        )

    if args.out_json:
        out_path = Path(args.out_json).expanduser()
        payload = {
            "submit_url": submit_url,
            "dataset_root": str(dataset_root),
            "seed": seed,
            "n": n,
            "max_workers": max_workers,
            "wait_s": float(args.wait_s),
            "timeout_s": float(args.timeout_s),
            "results": [
                {
                    "thread_dir": r.thread_dir,
                    "job_id": r.job_id,
                    "status": r.status,
                    "reward": r.reward,
                    "submit_s": r.submit_s,
                    "total_s": r.total_s,
                    "msg": r.msg,
                    "recalc_s": r.recalc_s,
                    "recalc_bytes": r.recalc_bytes,
                    "recalc_msg": r.recalc_msg,
                }
                for r in finalized
            ],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
