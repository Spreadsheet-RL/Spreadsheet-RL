# Tests / diagnostics

These scripts are meant to exercise useful service-level behavior. The local
contract test uses a test-only unsupported-host override and can run without
Excel; the end-to-end test should run on the Windows deployment machine with
Excel installed.

Run from the `reward/` directory:

- `python3 tests/01_api_contract.py`
- `python3 tests/02_windows_only_startup.py`
- `python3 tests/03_api_end_to_end.py --platform windows --workers 2`
- `python3 tests/04_answer_position_parsing.py`
- `python3 tests/05_deployed_endpoint_random_samples.py --dataset-root D:\path\to\spreadsheet-rl\output --n 16 --max-workers 4 --submit-url https://<reward-host>/reward/submit`
- `python3 tests/06_job_store_counters.py`
- `python3 tests/07_result_poll_config.py`

If you're using `uv`, you can also do:

- `uv run python3 tests/01_api_contract.py`
- `uv run python3 tests/02_windows_only_startup.py`
- `uv run python3 tests/03_api_end_to_end.py --platform windows --workers 2`
- `uv run python3 tests/04_answer_position_parsing.py`
- `uv run python3 tests/05_deployed_endpoint_random_samples.py --dataset-root D:\path\to\spreadsheet-rl\output --n 16 --max-workers 4 --submit-url https://<reward-host>/reward/submit`
- `uv run python3 tests/06_job_store_counters.py`
- `uv run python3 tests/07_result_poll_config.py`
