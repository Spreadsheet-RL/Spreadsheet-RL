import json
import threading
import time
from zipfile import ZipFile

import pytest

from resources_servers.spreadsheet.service import workbook
from resources_servers.spreadsheet.service.catalog import Catalog, sha256_file
from resources_servers.spreadsheet.service.errors import ConflictError, EnvironmentError, ToolError
from resources_servers.spreadsheet.service.state import Store


def fake_archive(path, marker="original"):
    # Transaction tests deliberately use a minimal archive and a fake tool engine.
    # Actual XLSX/LibreOffice semantics are tested in test_workbook.py on Linux.
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", marker)


class Engine:
    version = "test-double-not-libreoffice"

    def __init__(self):
        self.fail = False

    def recalculate(self, path):
        if self.fail:
            raise EnvironmentError("Engine unavailable")

    def probe(self):
        pass

    def close(self):
        pass


class Runner:
    def execute(self, workspace, code):
        fake_archive(workspace / "data.xlsx", "python mutation")
        raise ToolError("Program failed after writing")

    def probe(self):
        pass


@pytest.fixture
def state(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    fake_archive(data / "output.xlsx")
    fake_archive(data / "target.xlsx", "oracle")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "task_id": "task-A",
                "seed": "output.xlsx",
                "oracle": "target.xlsx",
                "answer_position": "Sheet1!A1",
                "dataset_revision": "test-commit",
                "seed_sha256": sha256_file(data / "output.xlsx"),
                "oracle_sha256": sha256_file(data / "target.xlsx"),
                "libreoffice_qualified": True,
            }
        )
        + "\n"
    )
    catalog = Catalog(manifest, data)
    store = Store(tmp_path / "state", catalog, Engine(), Runner(), workers=4)
    yield store
    store.shutdown()


def wait(store, receipt):
    deadline = time.monotonic() + 5
    while True:
        job = store.job(receipt["job_id"])
        if job["status"] == "DONE":
            return job["result"]
        if time.monotonic() > deadline:
            raise AssertionError("Job did not finish")
        time.sleep(0.005)


def test_eight_rollouts_never_share_mutable_seed(state, monkeypatch):
    sessions = [state.seed(f"rollout-{i}", "task-A")["session_id"] for i in range(8)]
    original = sha256_file(state.catalog.get("task-A").seed)

    def mutate(path, action, args):
        fake_archive(path, "edited")
        return {"changed": True}

    monkeypatch.setattr(workbook, "execute", mutate)
    result = wait(state, state.submit(sessions[0], "call-A", "write_range", {"range": "A1", "data": 7}))
    assert result["ok"]
    assert sha256_file(state.workspace(sessions[0]) / "data.xlsx") != original
    assert all(sha256_file(state.workspace(sid) / "data.xlsx") == original for sid in sessions[1:])
    assert sha256_file(state.catalog.get("task-A").seed) == original


def test_retry_is_once_but_identical_new_call_is_real(state, monkeypatch):
    count = []

    def mutate(path, action, args):
        count.append(1)
        fake_archive(path, str(len(count)))
        return {}

    monkeypatch.setattr(workbook, "execute", mutate)
    sid = state.seed("r", "task-A")["session_id"]
    first = state.submit(sid, "call-1", "delete_rows", {"rows": ["2"]})
    wait(state, first)
    retried = state.submit(sid, "call-1", "delete_rows", {"rows": ["2"]})
    assert retried["job_id"] == first["job_id"]
    assert len(count) == 1
    wait(state, state.submit(sid, "call-2", "delete_rows", {"rows": ["2"]}))
    assert len(count) == 2
    with pytest.raises(ConflictError):
        state.submit(sid, "call-1", "delete_rows", {"rows": ["3"]})


def test_failed_python_is_rolled_back(state):
    sid = state.seed("r", "task-A")["session_id"]
    path = state.workspace(sid) / "data.xlsx"
    before = sha256_file(path)
    result = wait(state, state.submit(sid, "python-1", "code_interpreter", {"code": "raise ValueError()"}))
    assert result["error_type"] == "tool_error"
    assert sha256_file(path) == before


def test_engine_failure_does_not_become_reward_zero(state):
    sid = state.seed("r", "task-A")["session_id"]
    state.engine.fail = True
    result = wait(state, state.submit(sid, "verify", "verify", {}))
    assert result["error_type"] == "environment_error"
    assert "reward" not in result
    assert state.session(sid)["state"] == "OPEN"


def test_verification_freezes_and_retries_return_same_receipt(state, monkeypatch):
    monkeypatch.setattr(workbook, "compare", lambda *args: {"reward": 1.0, "cell_accuracy": 1.0})
    sid = state.seed("r", "task-A")["session_id"]
    receipt = state.submit(sid, "verify", "verify", {})
    assert wait(state, receipt)["output"]["reward"] == 1
    assert state.session(sid)["state"] == "VERIFIED"
    assert state.submit(sid, "verify", "verify", {})["job_id"] == receipt["job_id"]
    with pytest.raises(ConflictError):
        state.submit(sid, "late-edit", "write_range", {"range": "A1", "data": 0})


def test_recovery_never_replays_indeterminate_write(state):
    sid = state.seed("r", "task-A")["session_id"]
    with state.db() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
            ("interrupted", sid, "call", "delete_rows", '{"rows":["2"]}', "RUNNING", None, time.time()),
        )
    state.recover()
    assert state.session(sid)["state"] == "INDETERMINATE"
    assert state.job("interrupted")["result"]["error_type"] == "environment_error"
    with pytest.raises(ConflictError):
        state.submit(sid, "new-call", "list_sheets", {})


def test_agent_execution_claim_prevents_inference_restart(state):
    sid = state.seed("r", "task-A")["session_id"]
    assert state.claim_execution(sid, "execution-A")["claimed"]
    assert state.claim_execution(sid, "execution-A")["claimed"]
    with pytest.raises(ConflictError):
        state.claim_execution(sid, "execution-B")
    with pytest.raises(ConflictError):
        state.seed("r", "task-A")


def test_close_and_cleanup_retain_active_jobs(state, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(workbook, "execute", slow)
    sid = state.seed("r", "task-A")["session_id"]
    receipt = state.submit(sid, "read", "list_sheets", {})
    assert entered.wait(2)
    with state.db() as db:
        db.execute("UPDATE sessions SET updated=0 WHERE id=?", (sid,))
    # It is enough to prove the receipt and workspace persist while the job runs.
    assert state.job(receipt["job_id"])["status"] == "RUNNING"
    assert state.workspace(sid).exists()
    release.set()
    wait(state, receipt)
    state.close_session(sid)
    with state.db() as db:
        db.execute("UPDATE sessions SET updated=0 WHERE id=?", (sid,))
    state.cleanup(60)
    assert not state.workspace(sid).exists()


def test_catalog_rejects_traversal_unqualified_and_changed_seed(state, tmp_path):
    row = json.loads((tmp_path / "manifest.jsonl").read_text())
    row["libreoffice_qualified"] = False
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="qualification"):
        Catalog(invalid, tmp_path / "data")
    row.update(libreoffice_qualified=True, seed="../data/output.xlsx")
    invalid.write_text(json.dumps(row))
    # A normalized path that stays under the configured root is acceptable;
    # escaping it to the actual private sibling is not.
    row["seed"] = "../../private.xlsx"
    invalid.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="escapes"):
        Catalog(invalid, tmp_path / "data")
    fake_archive(state.catalog.get("task-A").seed, "changed")
    with pytest.raises(EnvironmentError):
        state.seed("different-rollout", "task-A")


def test_symlink_python_output_cannot_replace_canonical_file(state):
    class SymlinkRunner:
        def execute(self, workspace, code):
            (workspace / "data.xlsx").unlink()
            (workspace / "data.xlsx").symlink_to(state.catalog.get("task-A").oracle)
            return {}

    state.python_runner = SymlinkRunner()
    sid = state.seed("r", "task-A")["session_id"]
    before = sha256_file(state.workspace(sid) / "data.xlsx")
    result = wait(state, state.submit(sid, "p", "code_interpreter", {"code": "pass"}))
    assert result["error_type"] == "tool_error"
    assert sha256_file(state.workspace(sid) / "data.xlsx") == before
