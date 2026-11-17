import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from resources_servers.spreadsheet.packaging import install_gym_integration as overlay
from resources_servers.spreadsheet.packaging import prepare_training_node as setup
from resources_servers.spreadsheet.packaging import start_service
from resources_servers.spreadsheet.schemas import TOOL_MODELS


def test_shared_private_export_preserves_credentials_and_is_repeatable(tmp_path, capsys):
    original = tmp_path / "service/private.yaml"
    original.parent.mkdir()
    original.write_text("fixture-private-key-value")
    shared = tmp_path / "shared/.secrets/gym.private.yaml"
    start_service.export_private_config(original, shared)
    start_service.export_private_config(original, shared)
    assert shared.read_bytes() == original.read_bytes()
    assert not shared.stat().st_mode & 0o077
    assert not capsys.readouterr().out
    original.write_text("changed-fixture-value")
    with pytest.raises(ValueError, match="refusing to replace"):
        start_service.export_private_config(original, shared)
    assert shared.read_text() == "fixture-private-key-value"


def test_shared_private_export_rejects_a_symlink(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text("fixture-value")
    target = tmp_path / "target.yaml"
    target.symlink_to(source)
    with pytest.raises(ValueError):
        start_service.export_private_config(source, target)


def test_setup_receipt_records_incomplete_phase_before_success(tmp_path):
    receipt = {"status": "applying", "phase": "reuse_existing_cache", "training_started": False}
    setup.save_receipt(tmp_path, receipt)
    assert json.loads((tmp_path / "setup.json").read_text()) == receipt
    assert not (tmp_path / "setup.json").stat().st_mode & 0o077
    receipt.update(status="prepared", phase="complete")
    setup.save_receipt(tmp_path, receipt)
    assert json.loads((tmp_path / "setup.json").read_text())["status"] == "prepared"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_cache_helper_runs_with_a_private_log_and_preserves_its_status(tmp_path, capsys, exit_code):
    config = tmp_path / "train config.yaml"
    config.touch()
    helper = tmp_path / "copy_gym_venvs.sh"
    helper.write_text(
        'test -f "$1" || exit 91\n'
        'test "$UV_OFFLINE:$PIP_NO_INDEX" = "1:1" || exit 92\n'
        'echo "fixture-private-stdout"\n'
        'echo "fixture-private-stderr" >&2\n'
        f"exit {exit_code}\n"
    )
    log = tmp_path / "cache-copy.private.log"
    previous_umask = os.umask(0)
    try:
        result = setup.run_cache_helper(helper, config, log)
    finally:
        os.umask(previous_umask)
    assert result.returncode == exit_code
    assert log.read_text().splitlines() == ["fixture-private-stdout", "fixture-private-stderr"]
    assert log.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_cache_helper_does_not_overwrite_an_existing_log(tmp_path):
    log = tmp_path / "cache-copy.private.log"
    log.write_text("original fixture evidence")
    with pytest.raises(FileExistsError):
        setup.run_cache_helper(tmp_path / "missing-helper.sh", tmp_path / "train.yaml", log)
    assert log.read_text() == "original fixture evidence"


def test_actual_cached_runtime_imports_the_spreadsheet_frontends(tmp_path):
    gym_root = Path(__file__).resolve().parents[3]
    venv = Path(sys.prefix)
    if not (venv / "bin/activate").is_file():
        pytest.skip("Existing activated venv is required for this import probe")
    if not (gym_root / "nemo_gym/base_resources_server.py").is_file():
        # The standalone fork is an overlay, not a full Gym checkout. Install
        # it into an isolated copy of the available real Gym source for probing.
        gym = pytest.importorskip("nemo_gym")
        from responses_api_agents.simple_agent import app as simple_agent

        source = gym_root
        gym_root = tmp_path / "Gym"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "cache", "results")
        shutil.copytree(Path(gym.__file__).parent, gym_root / "nemo_gym", ignore=ignore)
        shutil.copytree(Path(simple_agent.__file__).parent, gym_root / "responses_api_agents/simple_agent", ignore=ignore)
        overlay.install(source, gym_root)
    for name in ("resources_servers.spreadsheet.app", "responses_api_agents.spreadsheet_agent.app"):
        result = setup.probe_venv(venv, gym_root, name)
        assert result["ok"]
        assert result["module_file"] == str(gym_root.joinpath(*name.split(".")).with_suffix(".py"))


@pytest.mark.parametrize("failure", [None, "engine", "tools"])
def test_training_probe_reaches_service_directly_and_checks_backend(tmp_path, monkeypatch, failure):
    key = "fixture-key-" * 4

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                result = {
                    "status": "ready",
                    "engine": "other" if failure == "engine" else "qualified-engine",
                    "python_isolation": "chroot_uid_seccomp",
                }
            else:
                if self.headers.get("Authorization") != "Bearer " + key:
                    self.send_error(401)
                    return
                result = {"tools": [] if failure == "tools" else list(TOOL_MODELS)}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    private = tmp_path / "gym.private.yaml"
    private.write_text(
        "spreadsheet_resources:\n  resources_servers:\n    spreadsheet:\n"
        f"      service_url: http://127.0.0.1:{server.server_port}\n"
        f"      api_key: {key}\n      session_secret_key: {key}\n"
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        if failure:
            with pytest.raises(ValueError, match="health|tool set"):
                setup.service_probe(private, "qualified-engine")
        else:
            assert setup.service_probe(private, "qualified-engine")["tool_count"] == 12
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_invalid_private_config_error_does_not_expose_its_contents(tmp_path):
    private = tmp_path / "private.yaml"
    private.write_text('fixture-secret: "unterminated')
    with pytest.raises(ValueError) as error:
        setup.service_probe(private, "engine")
    assert "fixture-secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_wrapper_uses_existing_activation_from_the_config_directory(tmp_path):
    scripts = tmp_path / "source"
    scripts.mkdir()
    wrapper = scripts / "prepare_training_node.sh"
    wrapper.write_bytes(Path(setup.__file__).with_suffix(".sh").read_bytes())
    (scripts / "prepare_training_node.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'active': os.getenv('FIXTURE_ACTIVE'), 'args': sys.argv[1:]}))\n"
    )
    trainer = tmp_path / "trainer"
    helper = trainer / "custom_utils/utils_training/use_custom_opt.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        'export FIXTURE_ACTIVE="$1:$2"\n'
        f'export PATH="{Path(sys.executable).parent}:$PATH"\n'
        'echo "fixture-private-activation-output"\n'
    )
    config = tmp_path / "configs/train.yaml"
    config.parent.mkdir()
    config.touch()
    result = subprocess.run(
        ["bash", str(wrapper), str(trainer), "/fixture/nltk", str(config), "--expected-hostname", "fixture-node"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["cwd"] == str(config.parent)
    assert report["active"] == str(trainer) + ":/fixture/nltk"
    assert report["args"][-2:] == ["--expected-hostname", "fixture-node"]
    assert "fixture-private-activation-output" not in result.stdout + result.stderr
