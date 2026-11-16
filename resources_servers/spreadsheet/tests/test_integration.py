"""Real service/HTTP/Python/LibreOffice canary; no model inference is simulated as real."""

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    not os.environ.get("SPREADSHEET_TEST_REAL_LO") or not os.environ.get("SPREADSHEET_TEST_ROOTFS"),
    reason="Requires the qualified Linux CPU runtime",
)


def test_real_service_python_native_tools_and_binary_reward(tmp_path):
    import openpyxl

    from resources_servers.spreadsheet.service.app import ServiceConfig, create_app
    from resources_servers.spreadsheet.service.catalog import sha256_file

    data = tmp_path / "dataset"
    data.mkdir()
    for name, value in (("output.xlsx", 0), ("target.xlsx", 42)):
        wb = openpyxl.Workbook()
        wb.active.title = "Entries"
        wb.active["A1"] = 21
        wb.create_sheet("Summary")["B2"] = value
        wb.save(data / name)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "task_id": "fixture-multi-sheet",
                "seed": "output.xlsx",
                "oracle": "target.xlsx",
                "answer_position": "Summary!B2",
                "dataset_revision": "synthetic-canary-v1",
                "seed_sha256": sha256_file(data / "output.xlsx"),
                "oracle_sha256": sha256_file(data / "target.xlsx"),
                "libreoffice_qualified": True,
            }
        )
    )
    key = tmp_path / "api-key"
    key.write_text("integration-fixture-key-" + "x" * 32)
    config = ServiceConfig(
        manifest=manifest,
        data_root=data,
        state_root=tmp_path / "state",
        api_key_file=key,
        python_rootfs=Path(os.environ["SPREADSHEET_TEST_ROOTFS"]),
        job_workers=4,
        libreoffice_workers=2,
    )
    before_seed, before_oracle = sha256_file(data / "output.xlsx"), sha256_file(data / "target.xlsx")
    with TestClient(create_app(config)) as client:
        client.headers["Authorization"] = "Bearer " + key.read_text()
        assert client.get("/health").json()["python_isolation"] == "chroot_uid_seccomp"
        sid = client.post("/sessions", json={"rollout_id": "canary-1", "task_id": "fixture-multi-sheet"}).json()[
            "session_id"
        ]
        other = client.post("/sessions", json={"rollout_id": "canary-2", "task_id": "fixture-multi-sheet"}).json()[
            "session_id"
        ]

        def operation(session, operation_id, action, arguments):
            response = client.post(
                f"/sessions/{session}/operations",
                json={
                    "operation_id": operation_id,
                    "action": action,
                    "arguments": arguments,
                },
            )
            assert response.status_code == 200, response.text
            job = response.json()
            deadline = time.monotonic() + 120
            while job["status"] != "DONE":
                assert time.monotonic() < deadline
                time.sleep(0.05)
                job = client.get("/jobs/" + job["job_id"]).json()
            assert job["result"]["ok"], job
            return job["result"]

        operation(
            sid,
            "python-1",
            "code_interpreter",
            {
                "code": (
                    "from openpyxl import load_workbook\n"
                    "wb=load_workbook('data.xlsx')\n"
                    "wb['Summary']['B2']='=Entries!A1*2'\n"
                    "wb.save('data.xlsx')\n"
                )
            },
        )
        read = operation(sid, "recalc-1", "recalculate_and_read", {"cell_ranges": ["Summary!B2"]})
        assert read["output"]["ranges"][0]["cells"][0]["value"] == 42
        assert operation(sid, "verify", "verify", {})["output"]["reward"] == 1
        assert operation(other, "verify", "verify", {})["output"]["reward"] == 0
        assert sha256_file(data / "output.xlsx") == before_seed
        assert sha256_file(data / "target.xlsx") == before_oracle
