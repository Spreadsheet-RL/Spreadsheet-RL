# Tests / diagnostics

These scripts are meant to exercise useful service-level behavior. The local
contract test uses a test-only unsupported-host override and can run without
Excel; the end-to-end test should run on the Windows deployment machine with
Excel installed.

Run from the `reward/` directory with `uv`:

- `uv run python tests/01_api_contract.py`
- `uv run python tests/02_windows_only_startup.py`
- `uv run python tests/03_api_end_to_end.py --platform windows --workers 2`
- `uv run python tests/04_answer_position_parsing.py`
- `uv run python tests/05_deployed_endpoint_random_samples.py --dataset-root D:\path\to\spreadsheet-rl\data --n 16 --max-workers 4 --submit-url https://reward.example.com/reward/submit --confirm-production`
- `uv run python tests/06_job_store_counters.py`
- `uv run python tests/07_result_poll_config.py`
- `uv run python tests/08_worker_response_handling.py`
- `uv run python tests/09_manager_startup_recovery.py`
- `uv run python tests/10_submit_error_paths.py`
- `uv run python tests/11_inflight_cache.py`
- `uv run python tests/12_run_job_cancellation_requeue.py`
