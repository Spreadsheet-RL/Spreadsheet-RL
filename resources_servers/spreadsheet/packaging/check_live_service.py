"""Trusted API canary, separate from model rollouts; touches only two new sessions."""

import argparse
import hashlib
import json
import math
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


class ServiceHTTPError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Service HTTP {code}")


class Client:
    def __init__(self, private=None, *, service_config=None):
        if service_config is not None:
            config = json.loads(Path(service_config).read_text())
            self.url = f"http://127.0.0.1:{int(config['port'])}"
            self.key = Path(config["api_key_file"]).read_text().strip()
        else:
            from omegaconf import OmegaConf

            config = OmegaConf.load(private).spreadsheet_resources.resources_servers.spreadsheet
            self.url, self.key = str(config.service_url).rstrip("/"), str(config.api_key)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.operations = []

    def request(self, method, path, body=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ServiceHTTPError(error.code) from None

    def operation(self, session, action, arguments, operation_id=None):
        operation_id = operation_id or uuid4().hex
        record = {"session": session, "action": action, "operation_id": operation_id, "status": "submitted"}
        self.operations.append(record)
        job = self.request(
            "POST",
            f"/sessions/{session}/operations",
            {
                "operation_id": operation_id,
                "action": action,
                "arguments": arguments,
            },
        )
        deadline = time.monotonic() + 300
        while job["status"] != "DONE":
            if time.monotonic() >= deadline:
                raise TimeoutError("Service canary operation deadline reached")
            time.sleep(0.1)
            job = self.request("GET", "/jobs/" + job["job_id"])
        result = job["result"]
        if not result.get("ok"):
            record.update(status="failed", error_type=result.get("error_type"))
            raise RuntimeError(f"Canary {action} failed: {result.get('error_type', 'unknown')}")
        record["status"] = "passed"
        print(json.dumps({"event": "SERVICE_CANARY_TOOL_OK", "action": action}), flush=True)
        return result["output"]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cell(output):
    return output["ranges"][0]["cells"][0]["value"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--private-config", type=Path)
    credentials.add_argument("--service-config", type=Path)
    for name in ("dataset", "manifest", "data-root", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong instance")
    if args.output_dir.exists():
        parser.error("Choose a fresh output directory")
    args.output_dir.mkdir(mode=0o700)
    with args.dataset.open() as stream:
        task_id = json.loads(next(stream))["task_id"]
    task = next(
        json.loads(line) for line in args.manifest.read_text().splitlines() if json.loads(line)["task_id"] == task_id
    )
    originals = []
    for kind in ("seed", "oracle"):
        path = (args.data_root / task[kind]).resolve()
        if not path.is_relative_to(args.data_root.resolve()):
            raise ValueError("Catalog path leaves data root")
        before = digest(path)
        if before != task[kind + "_sha256"]:
            raise ValueError("Original workbook differs from the qualified catalog")
        originals.append((path, before))
    client = Client(args.private_config, service_config=args.service_config)
    report = {
        "actor": "trusted_service_canary",
        "status": "running",
        "hostname": socket.gethostname(),
        "task_id": task_id,
        "operations": client.operations,
        "checks": {},
        "closed_sessions": [],
    }
    sessions = []
    sheet = "qa_" + uuid4().hex[:12]
    try:
        report["health"] = client.request("GET", "/health")
        assert report["health"]["status"] == "ready"
        assert report["health"]["python_isolation"] == "chroot_uid_seccomp"
        try:
            client.request("GET", "/tools", auth=False)
        except ServiceHTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("Anonymous tool discovery was accepted")
        report["checks"]["authentication"] = True
        advertised = set(client.request("GET", "/tools")["tools"])
        for _ in range(2):
            sessions.append(
                client.request("POST", "/sessions", {"rollout_id": "qa-" + uuid4().hex, "task_id": task_id})[
                    "session_id"
                ]
            )
        baseline, edited = sessions
        original_sheets = client.operation(baseline, "list_sheets", {})["sheets"]
        client.operation(edited, "manage_sheet", {"action": "create", "sheet_name": sheet})
        other_sheets = client.operation(baseline, "list_sheets", {})["sheets"]
        assert sheet not in {item["name"] for item in other_sheets}
        assert {item["name"] for item in original_sheets} == {item["name"] for item in other_sheets}
        report["checks"]["rollout_isolation"] = True
        client.operation(
            edited, "write_range", {"sheet_name": sheet, "range": "A1:B3", "data": [[1, 2], [20, 30], [100, 200]]}
        )
        client.operation(
            edited, "fill_formula", {"sheet_name": sheet, "start_cell": "C1", "formula_template": "=A1+B1"}
        )
        assert cell(client.operation(edited, "recalculate_and_read", {"cell_ranges": [sheet + "!C1"]})) == 3
        client.operation(
            edited, "format_range", {"sheet_name": sheet, "range": "A1:C1", "bold": True, "fill_color": "yellow"}
        )
        code = (
            "import os, socket\n"
            "from openpyxl import load_workbook\n"
            "assert os.geteuid() != 0\n"
            "assert not os.path.exists('/proc')\n"
            "assert not os.path.exists('/workspace/storage-shared')\n"
            "try:\n    sock=socket.socket()\n"
            "except PermissionError:\n    pass\n"
            "else:\n    sock.close(); raise AssertionError('network socket was permitted')\n"
            "wb=load_workbook('data.xlsx')\n"
            f"wb[{json.dumps(sheet)}]['B1']=10\n"
            "wb.save('data.xlsx')\nprint('QA_PYTHON_OK')\n"
        )
        client.operation(edited, "code_interpreter", {"code": code})
        assert cell(client.operation(edited, "recalculate_and_read", {"cell_ranges": [sheet + "!C1"]})) == 11
        report["checks"].update(python_isolation=True, libreoffice_recalculation=True)
        assert client.operation(
            edited, "find_cells", {"sheet_name": sheet, "range": "A1:C3", "query": "11", "match": "equals"}
        )["matches"]
        replay = uuid4().hex
        row_args = {"sheet_name": sheet, "rows": ["2"]}
        client.operation(edited, "delete_rows", row_args, replay)
        client.operation(edited, "delete_rows", row_args, replay)
        assert cell(client.operation(edited, "inspect_range", {"sheet_name": sheet, "range": "A2"})) == 100
        report["checks"]["operation_replay"] = True
        client.operation(edited, "delete_columns", {"sheet_name": sheet, "columns": ["B:C"]})
        client.operation(edited, "clear_range", {"sheet_name": sheet, "range": "A2"})
        assert cell(client.operation(edited, "inspect_range", {"sheet_name": sheet, "range": "A2"})) is None
        client.operation(edited, "manage_sheet", {"action": "delete", "sheet_name": sheet})
        scores = [client.operation(session, "verify", {}) for session in sessions]
        assert all(math.isfinite(float(score["reward"])) and score["reward"] in (0, 1) for score in scores)
        assert scores[0]["reward"] == scores[1]["reward"]
        assert scores[0]["cell_accuracy"] == scores[1]["cell_accuracy"]
        report["scores"] = scores
        report["checks"]["reward_preserved_after_qa_sheet_removed"] = True
        assert all(digest(path) == before for path, before in originals)
        report["checks"]["seed_and_oracle_unchanged"] = True
        covered = {item["action"] for item in client.operations} - {"verify"}
        assert covered == advertised
        report["tool_count"] = len(covered)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        for session in sessions:
            try:
                client.request("POST", f"/sessions/{session}/close", {})
                report["closed_sessions"].append(session)
            except Exception:
                report.setdefault("unconfirmed_close", []).append(session)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "LIVE_SERVICE_CANARY_PASSED",
                "tool_count": report["tool_count"],
                "checks": report["checks"],
                "scores": report["scores"],
                "report": str(args.output_dir / "report.json"),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
