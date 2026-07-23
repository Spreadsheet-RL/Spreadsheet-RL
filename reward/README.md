# Async Spreadsheet Reward API

FastAPI service for spreadsheet rewards designed for Windows Excel COM automation
behind proxy/tunnel environments (e.g. Cloudflare), where long-running HTTP
requests may be dropped.

Instead of computing the reward synchronously, clients:

1. `POST /reward/submit` to upload a workbook and receive a `job_id` quickly.
2. Poll `GET /reward/result/{job_id}` until the result is ready.

The service is Windows-only. It can run multiple jobs concurrently by increasing
`--workers` and/or `--instance-per-worker` (persistent Excel pool, enabled by
default).

## Install

Requires `uv` (https://astral.sh/uv).

From the `reward/` directory in PowerShell:

```powershell
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
uv sync --locked
```

## Run

Multi-worker deployments are safe (job state is shared via SQLite), so submit/poll
requests can land on any worker without 404s.

Run:

```powershell
$env:REWARD_API_OUTPUT_ROOT = 'D:\path\to\spreadsheet-rl\data'  # extracted dataset root
$env:REWARD_API_DB_PATH = 'D:\path\to\spreadsheet-rl\db\jobs.sqlite3'  # optional override
$env:REWARD_API_RECALC_JOB_ROOT = 'D:\path\to\spreadsheet-rl\recalc-jobs'
$env:REWARD_API_MAX_UPLOAD_BYTES = '104857600'  # 100 MiB API limit
$env:REWARD_API_WINDOWS_EXCEL_RECYCLE_PRIVATE_MB = '4096'  # optional; set 0 to disable
$env:REWARD_API_WINDOWS_EXCEL_RECYCLE_JOBS = '0'  # optional; set >0 to recycle periodically
uv run async-reward-api --platform windows --host 127.0.0.1 --port 5000 --workers 2 --instance-per-worker 2
```

Concurrency notes:

- Increasing `--workers` starts multiple API workers.
- Increasing `--instance-per-worker` starts persistent Excel instances per API worker.
- Total Excel instances is roughly `--workers * --instance-per-worker`.
- The example above starts four persistent Excel instances total.
- Pool workers start concurrently, so startup time is bounded by the slowest worker.
- Set `--instance-per-worker 0` to disable the persistent pool.
- Excel instances are memory-heavy; if you see OOMs, reduce concurrency or set
  `REWARD_API_MAX_RUNNING_JOBS` to cap global running jobs across all workers.

Excel failure handling:

- The persistent pool treats `fatal worker error` results as unhealthy Excel
  sessions and replaces the worker instead of returning it to the pool.
- Ordinary workbook/recalc failures remain job failures and do not recycle a
  healthy worker.
- If Excel returns no workbook from `Workbooks.Open`, the API attempts to
  identify newly opened workbooks by path before failing the job.
- Graceful shutdown or task cancellation requeues claimed in-flight jobs when
  possible, retaining the uploaded workbook for retry. If requeue fails, the job
  is marked `ERROR` as a last resort.
- Invalid persisted job rows are quarantined at startup and during the periodic
  stale-running sweep, not on every submit/poll request.
- Per-job fallback Excel killing is reserved for timeouts and generic worker
  crashes; controlled fatal recalc exits use attributed PID cleanup only.

The command above binds to loopback for a local proxy or tunnel. For direct LAN
access, use `--host 0.0.0.0` and open the firewall port (run as Administrator):

```powershell
New-NetFirewallRule -DisplayName "Async Reward API" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000 -Profile Private -RemoteAddress LocalSubnet
```

The service has no application-level authentication. Do not expose it directly
to the public internet: use an authenticated, rate-limited proxy or tunnel. Set
both a finite upstream request-body limit and `REWARD_API_MAX_UPLOAD_BYTES`;
the API limit is enforced while copying an already parsed multipart upload and
does not protect the proxy or temporary spool from oversized request bodies.

## Endpoints

- `GET /health`
- `POST /reward/submit` (or `POST /reward`) -> `{ job_id, status, status_path, result_path }` for `.xlsx` samples; unsupported
  primary sample types return `400`.
- `POST /recalculate/submit` (or `POST /recalculate`) -> `{ job_id, status, status_path, result_path }`
- `GET /reward/status/{job_id}`
- `GET /reward/result/{job_id}?wait_s=10` (optional short wait for fewer polls; capped at 25 seconds)
- `GET /recalculate/status/{job_id}`
- `GET /recalculate/result/{job_id}?wait_s=10` -> returns `.xlsx` bytes when done (otherwise JSON status;
  `wait_s` is capped at 25 seconds)

## Environment variables

Path-valued variables expand `~` and environment variables (e.g. `%USERPROFILE%`).

- `REWARD_API_OUTPUT_ROOT` (optional): extracted dataset root used to resolve each
  submitted `thread_dir`; reward jobs require its matching `instruction.json` and
  oracle `target.xlsx` (default: `%USERPROFILE%\async_reward_api_output`). Keep it
  stable while reward jobs are queued or running in the shared database.
- `REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR` (Windows-only): when set, delete all contents of this directory every
  hour. The cleanup target must stay under `%LOCALAPPDATA%\Temp\Diagnostics`; cleanup is disabled by default.
- `REWARD_API_DB_PATH` (optional): SQLite job store path (default: `~/.async_reward_api/jobs.sqlite3`).
- `REWARD_API_SQLITE_TIMEOUT_S` (default: `30`): SQLite busy timeout for each connection.
- `REWARD_API_SQLITE_EXECUTOR_WORKERS` (default: `2`): dedicated per-process thread count for SQLite operations
  (clamped to `1..8`). This keeps database work isolated from upload, file-cleanup, and fallback-worker threads.
- `REWARD_API_WORKER_TIMEOUT_S` (default: `240`): hard timeout for a single recalc+eval job.
- `REWARD_API_INSTANCE_PER_WORKER` (Windows-only, default: `1`): persistent Excel instances per API worker (set `0` to
  disable the pool). Prefer the CLI flag `--instance-per-worker`.
- `REWARD_API_WINDOWS_EXCEL_RECYCLE_PRIVATE_MB` (Windows-only, default: `4096`): recycle an Excel pool worker after a job
  if the Excel process private bytes is at/above this threshold (set `0` to disable). Recycling is scheduled in the
  background, so the job result is not delayed.
- `REWARD_API_WINDOWS_EXCEL_RECYCLE_JOBS` (Windows-only, default: `0`): recycle an Excel pool worker after it has run this
  many jobs (set `0` to disable).
- `REWARD_API_MAX_QUEUE_SIZE` (default: `3000`): max queued jobs; if exceeded, submit returns `busy`.
- `REWARD_API_MAX_UPLOAD_BYTES` (optional): when set to a positive byte count, both reward and recalculate submit
  endpoints reject oversized uploads with HTTP 413 while copying the request body. Also enforce a request-body
  limit at the proxy because multipart parsing and temporary spooling happen before this check.
- `REWARD_API_POLL_INTERVAL_S` (default: `0.2`, clamped to `0.05..5.0`): base interval for empty-queue worker
  polling and the compatibility fallback for initial result polling.
- `REWARD_API_IDLE_POLL_MAX_S` (configured default: `0.5`): max backoff interval for empty-queue worker
  polling; the effective value is never below the base poll interval. Lower values reduce cross-process dispatch
  latency when another Uvicorn worker accepts a job.
- `REWARD_API_RESULT_POLL_INTERVAL_S` (optional): initial server-side result long-poll interval; defaults to
  `REWARD_API_POLL_INTERVAL_S` for compatibility, or `0.2` when unset.
- `REWARD_API_RESULT_POLL_MAX_S`: max adaptive backoff interval for server-side result long-polling. When unset,
  it defaults to at least `1.0` and never below the initial result-poll interval. If only the legacy
  `REWARD_API_POLL_INTERVAL_S` is set, result polling stays at that fixed interval unless this is set.
- `REWARD_API_JOB_TTL_S` (default: `3600`): how long to keep completed job results in SQLite.
- `REWARD_API_CLEANUP_BATCH_SIZE` (default: `512`): max finished jobs processed per cleanup pass (limits cleanup memory/latency spikes).
- `REWARD_API_CLEANUP_MAX_BATCHES` (default: `8`): max cleanup batches attempted per pass before waiting for the next interval.
- `REWARD_API_CLEANUP_LEADER_LEASE_S` (default: `900`): SQLite lease duration used to elect a single cleanup leader across multiple API workers.
- `REWARD_API_CLEANUP_RETRY_AFTER_S` (default: `300`): delay before retrying failed cleanup deletions.
- `REWARD_API_CLEANUP_RETRY_MAX_S` (default: `3600`): upper bound for exponential cleanup retry backoff.
- `REWARD_API_CLEANUP_RETRY_BATCH_SHARE` (default: `0.25`): fraction of each cleanup batch reserved for retry-eligible rows to avoid retry starvation under sustained new-job load.
- `REWARD_API_STALE_SWEEP_LEADER_LEASE_S` (default: `30`): short SQLite lease used to avoid duplicate stale-running sweeps across workers without delaying recovery behind the longer cleanup lease.
- `REWARD_API_QUARANTINE_SWEEP_INTERVAL_S` (default: `600`): minimum interval between full-table invalid-row quarantine scans, coordinated across workers via a SQLite lease. Set `0` to run every stale-sweep pass under the stale-sweep lease.
- `REWARD_API_HEALTH_FAILURE_TTL_S` (default: `600`): readiness degradation from transient job-task or background-loop failures recovers after this many seconds. Set `0` to keep degradation sticky until restart.
- `REWARD_API_SAMPLE_META_CACHE_SIZE` (default: `2048`): max cached `thread_dir` metadata entries for submit path.
- `REWARD_API_GT_CACHE_SIZE` (default: `128`): max cached ground-truth workbook payloads used during reward comparison.
- `REWARD_API_GT_CACHE_MAX_CELLS` (default: `500000`): total prepared ground-truth cell budget per evaluation
  process. Weighted LRU eviction keeps the cache under this limit, and a single entry larger than the budget is not
  cached. Set `0` to disable the prepared ground-truth cache.
- `REWARD_API_GT_PREPARED_MAX_CELLS` (default: `500000`): if `answer_position` covers more cells than this,
  comparison switches to streaming mode and skips prepared GT caching.
- `REWARD_API_KEEP_FILES` (default: `0`): keep uploaded workbooks on disk for debugging. Do not enable this in
  production without separate retention cleanup: TTL cleanup removes expired database rows but intentionally
  leaves their workbook files behind.
- `REWARD_API_RECALC_JOB_ROOT` (optional): where to store `/recalculate` job files (default: OS temp dir under
  `async_reward_api_recalculate_jobs`). This path is a security anchor for persisted recalculate jobs: result
  retrieval and cleanup only trust paths under the currently configured root. Keep it stable until every row that
  references it has expired or been migrated, including completed results retained for their TTL.
- `REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL` (default: `0`): if set to `1`, timeout or generic worker-failure recovery may kill newly appeared Excel PIDs (baseline-diff fallback) when direct worker PID attribution is unavailable. Controlled fatal recalc exits do not use this fallback.
- `REWARD_API_MAX_RUNNING_JOBS` (optional): global cap on concurrent running jobs across all Uvicorn workers
  (unlimited except when `REWARD_API_INSTANCE_PER_WORKER=0`, where it defaults to `1` for safer non-pooled execution).

`/health` reports queue counts, bounded Excel pool status, background task
liveness, a per-process `instance_id`, and a `db_fingerprint` hash for checking
whether workers are sharing the same job store. It intentionally does not expose
local filesystem paths, hostnames, process IDs, or raw worker identifiers.

Treat `status: ok` as process readiness. If a required background loop exits,
the configured Excel pool cannot start, or a running pool has no live Excel
workers, `/health` returns `status: degraded` with HTTP 503. Readiness monitors
should also check `excel_pool.startup_failed`, `excel_pool.alive_instances`, and
the effective `max_running_jobs`. `available_instances` is queue telemetry, not
a hard capacity invariant.

## Client (Spreadsheet-RL)

Point Spreadsheet-RL to the submit endpoint:

```bash
export SPREADSHEET_RL_REWARD_URL="https://<your-domain>/reward/submit"
# or: export SPREADSHEET_RL_REWARD_URL="https://<your-domain>/reward"
export SPREADSHEET_RL_REWARD_TIMEOUT_S=300
```

`verl.utils.reward_score.sheet_arena.compute_score` will submit and poll until
the overall timeout is reached, then fall back to `0.0`.
