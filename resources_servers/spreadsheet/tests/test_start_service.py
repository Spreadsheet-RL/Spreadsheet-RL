"""Lightweight launcher checks; target-node startup exercises actual LO/chroot."""

import json
import sys
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from resources_servers.spreadsheet.packaging import start_service as launcher


def test_pid_reuse_requires_the_exact_service_module_and_config(tmp_path):
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    config = Path("/opt/service/private/service.json")
    cmdline = proc / "cmdline"
    cmdline.write_bytes(b"python\0-m\0unrelated.module\0--config\0/opt/service/private/service.json\0")
    assert not launcher.owned_process(123, config, tmp_path / "proc")
    cmdline.write_bytes(b"python\0-m\0resources_servers.spreadsheet.service.app\0--config\0/wrong/service.json\0")
    assert not launcher.owned_process(123, config, tmp_path / "proc")
    cmdline.write_bytes(
        b"python\0-m\0resources_servers.spreadsheet.service.app\0--config\0/opt/service/private/service.json\0"
    )
    assert launcher.owned_process(123, config, tmp_path / "proc")
    assert not launcher.owned_process(999, config, tmp_path / "proc")
    assert not launcher.owned_process("123", config, tmp_path / "proc")


@pytest.mark.parametrize(
    "change", [{"manifest": "/other/catalog"}, {"python_isolation": "unsafe"}, {"allow_unqualified_tasks": True}]
)
def test_configuration_changes_are_rejected_without_rewriting_files(tmp_path, change):
    expected = {"manifest": "/qualified/catalog", "port": 8088}
    value = {**expected, "python_isolation": "chroot_uid_seccomp", **change}
    path = tmp_path / "service.json"
    original = json.dumps(value)
    path.write_text(original)
    with pytest.raises(ValueError):
        launcher.validate_config(path, expected)
    assert path.read_text() == original


@pytest.mark.parametrize(
    "change",
    [{"service_url": "http://previous-node:8088"}, {"api_key": "different-key"}, {"session_secret_key": "short"}],
)
def test_existing_adapter_address_and_keys_are_validated_without_printing_secrets(tmp_path, change):
    values = {
        "service_url": "http://current-node:8088",
        "api_key": "fixture-service-key",
        "session_secret_key": "fixture-session-key-" + "x" * 32,
    }
    values.update(change)
    path = tmp_path / "gym.private.yaml"
    text = "spreadsheet_resources:\n" + "".join(
        f"      {name}: {json.dumps(value)}\n" for name, value in values.items()
    )
    path.write_text(text)
    with pytest.raises(ValueError) as error:
        launcher.validate_adapter_config(path, "http://current-node:8088", "fixture-service-key")
    assert "fixture-service-key" not in str(error.value)
    assert path.read_text() == text


def test_launcher_accepts_private_files_created_by_bundled_initializer(tmp_path, monkeypatch, capsys):
    from resources_servers.spreadsheet import init_service

    private = tmp_path / "private"
    arguments = ["init_service.py", "--service-url", "http://127.0.0.1:8088"]
    for flag, name in (
        ("data-root", "data"),
        ("manifest", "manifest.jsonl"),
        ("state-root", "state"),
        ("python-rootfs", "python-root"),
        ("config-dir", "private"),
    ):
        arguments.extend(["--" + flag, str(tmp_path / name)])
    monkeypatch.setattr(sys, "argv", arguments)
    init_service.main()
    key = (private / "api-key").read_text().strip()
    launcher.validate_adapter_config(private / "gym.private.yaml", "http://127.0.0.1:8088", key)
    assert len(key) >= 32
    assert key not in capsys.readouterr().out
    assert all(not path.stat().st_mode & 0o077 for path in private.iterdir())


@contextmanager
def running_http_fixture(*, engine="qualified-LO", allow_anonymous=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/health":
                status, body = 200, {"status": "ready", "engine": engine, "python_isolation": "chroot_uid_seccomp"}
            elif self.path == "/tools" and (
                allow_anonymous or self.headers.get("Authorization") == "Bearer private-fixture-key"
            ):
                status, body = 200, {"tools": ["tool-a", "tool-b"]}
            else:
                status, body = 401, {"detail": "unauthorized"}
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_tcp_readiness_checks_auth_engine_and_schema_without_using_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with running_http_fixture() as base:
        health, tools = launcher.probe(opener, base, "private-fixture-key", "qualified-LO", {"tool-a", "tool-b"})
        assert health["status"] == "ready"
        assert tools == ["tool-a", "tool-b"]
        with pytest.raises(ValueError, match="tool list"):
            launcher.probe(opener, base, "private-fixture-key", "qualified-LO", {"unexpected"})
        with pytest.raises(ValueError, match="qualified runtime"):
            launcher.probe(opener, base, "private-fixture-key", "different-LO", {"tool-a", "tool-b"})


def test_tcp_readiness_rejects_unauthenticated_tool_access():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with running_http_fixture(allow_anonymous=True) as base:
        with pytest.raises(ValueError, match="Unauthenticated"):
            launcher.probe(opener, base, "private-fixture-key", "qualified-LO", {"tool-a", "tool-b"})
