import ast
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from resources_servers.spreadsheet.packaging import build_source_bundle as bundle
from resources_servers.spreadsheet.packaging import convert_training_data as data
from resources_servers.spreadsheet.packaging import install_gym_integration as overlay


SOURCE = Path(__file__).resolve().parents[3]


def contract_fixture(tmp_path):
    root = tmp_path / "trainer"
    root.mkdir()
    source = root / "processor.py"
    source.write_text("def parse_row(row):\n    return row\n")
    expected = hashlib.sha256(
        ast.dump(ast.parse(source.read_text()).body[0], include_attributes=False).encode()
    ).hexdigest()
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps(
            {
                "contract_id": "fixture-raw-row-v1",
                "ast_python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "symbols": [{"path": "processor.py", "name": "parse_row", "normalized_ast_sha256": expected}],
            }
        )
    )
    return root, source, contract


def test_contract_preserves_documentation_changes_but_rejects_changed_row_semantics(tmp_path):
    root, source, contract = contract_fixture(tmp_path)
    before = data.verify_contract(root, contract)
    source.write_text('# changed documentation\ndef parse_row(row):\n    """Updated docs."""\n    return row\n')
    after = data.verify_contract(root, contract)
    assert before["sources"][0]["file_sha256"] != after["sources"][0]["file_sha256"]
    assert before["sources"][0]["normalized_ast_sha256"] == after["sources"][0]["normalized_ast_sha256"]
    source.write_text("def parse_row(row):\n    return row['response']\n")
    with pytest.raises(ValueError, match="SOURCE_CONTRACT_CHANGED"):
        data.verify_contract(root, contract)


def test_bad_contract_prevents_creating_data_or_editing_yaml(tmp_path, monkeypatch):
    root, source, contract = contract_fixture(tmp_path)
    source.write_text("def parse_row(row):\n    return None\n")
    config = tmp_path / "trainer.yaml"
    original = "data:\n  train:\n    - data_path: /old\n"
    config.write_text(original)
    output = tmp_path / "converted"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "convert_training_data.py",
            "--input-dir",
            str(tmp_path / "missing-input"),
            "--output-dir",
            str(output),
            "--trainer-root",
            str(root),
            "--contract",
            str(contract),
            "--trainer-config",
            str(config),
        ],
    )
    with pytest.raises(ValueError, match="SOURCE_CONTRACT_CHANGED"):
        data.main()
    assert not output.exists()
    assert config.read_text() == original


def test_conversion_run_receipts_preserve_provenance_on_repeated_runs(tmp_path, monkeypatch, capsys):
    qualified = tmp_path / "qualified"
    qualified.mkdir()
    for split in ("train", "test"):
        (qualified / f"{split}.qualified.jsonl").write_text(
            json.dumps(
                {
                    "task_id": split,
                    "dataset_revision": "fixture-revision",
                    "responses_create_params": {
                        "input": [{"role": "user", "content": "fixture"}],
                        "tools": [{"type": "function", "name": "list_sheets"}],
                    },
                }
            )
            + "\n"
        )
    output = tmp_path / "converted"
    monkeypatch.setattr(
        sys, "argv", ["convert_training_data.py", "--input-dir", str(qualified), "--output-dir", str(output)]
    )
    data.main()
    data.main()
    receipts = list(output.glob("conversion-run-*.json"))
    assert len(receipts) == 2
    for path in receipts:
        value = json.loads(path.read_text())
        assert value["script_sha256"] == data.digest(Path(data.__file__))
        assert value["conversion"]["rows"] == {"train": 1, "test": 1}
        assert value["conversion"]["outputs"]["train"] == data.digest(output / "train.jsonl")
        assert not path.stat().st_mode & 0o077
    assert "SP10_DATA_READY" in capsys.readouterr().out


def gym_fixture(tmp_path):
    root = tmp_path / "Gym"
    for relative in ("nemo_gym/base_resources_server.py", "responses_api_agents/simple_agent/app.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# pre-existing Gym interface fixture\n")
    return root


def test_overlay_installs_both_frontends_without_backend_or_other_source_changes(tmp_path):
    root = gym_fixture(tmp_path)
    original = (root / "responses_api_agents/simple_agent/app.py").read_bytes()
    files = overlay.install(SOURCE, root)
    assert len(files) == 6
    assert overlay.install(SOURCE, root) == files
    assert (root / "responses_api_agents/simple_agent/app.py").read_bytes() == original
    for relative, sha in files.items():
        assert data.digest(root / relative) == sha
    assert not (root / "resources_servers/spreadsheet/service").exists()


def test_overlay_checks_conflicts_before_installing_either_module(tmp_path):
    root = gym_fixture(tmp_path)
    existing = root / "responses_api_agents/spreadsheet_agent/app.py"
    existing.parent.mkdir()
    existing.write_text("# operator edits\n")
    with pytest.raises(ValueError, match="differs"):
        overlay.install(SOURCE, root)
    assert existing.read_text() == "# operator edits\n"
    assert not (root / "resources_servers/spreadsheet").exists()


def test_registering_configs_keeps_model_settings_and_hyperparameters_unchanged(tmp_path):
    config = tmp_path / "trainer.yaml"
    original = (
        "policy:\n  model_name: existing-model\ngrpo:\n  num_prompts_per_step: 128\n"
        "env:\n  nemo_gym:\n    config_paths:\n      - existing/model.yaml\n"
        "    # existing policy settings\n    policy_model:\n      num_workers: 16\n"
    )
    config.write_text(original)
    additions = [
        Path("/Gym/resources_servers/spreadsheet/configs/spreadsheet.yaml"),
        Path("/private/gym.private.yaml"),
    ]
    backup = Path(overlay.register_configs(config, additions))
    assert backup.read_text() == original
    added = "".join("      - " + json.dumps(str(path)) + "\n" for path in additions)
    assert config.read_text().replace(added, "") == original
    assert config.read_text().index(str(additions[0])) < config.read_text().index(str(additions[1]))
    assert overlay.register_configs(config, additions) is None


def test_overlay_receipt_and_output_do_not_contain_private_config_values(tmp_path, monkeypatch, capsys):
    root = gym_fixture(tmp_path)
    config = tmp_path / "trainer.yaml"
    config.write_text("env:\n  nemo_gym:\n    config_paths:\n      - existing/model.yaml\n")
    private = tmp_path / "gym.private.yaml"
    private.write_text("fixture-private-value-not-for-logs")
    private.chmod(0o600)
    receipt = tmp_path / "runs/install.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_gym_integration.py",
            "--source-root",
            str(SOURCE),
            "--gym-root",
            str(root),
            "--trainer-config",
            str(config),
            "--private-config",
            str(private),
            "--receipt",
            str(receipt),
        ],
    )
    overlay.main()
    assert private.read_text() not in receipt.read_text() + capsys.readouterr().out
    assert json.loads(receipt.read_text())["runtime_import_check"] == "not_run"


def test_source_bundle_is_repeatable_and_excludes_untracked_private_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    app = root / "resources_servers/spreadsheet/app.py"
    app.parent.mkdir(parents=True)
    app.write_text("# source fixture\n")
    (root / "LICENSE").write_text("source-license-fixture\n")
    (root / ".gitignore").write_text("*.env\n")
    subprocess.run(
        ["git", "add", "resources_servers/spreadsheet/app.py", "LICENSE", ".gitignore"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
    )
    (root / "private.env").write_text("fixture-private-value")
    first = bundle.build(root, tmp_path / "one")
    second = bundle.build(root, tmp_path / "two")
    assert first["sha256"] == second["sha256"]
    with tarfile.open(first["archive"]) as archive:
        assert not any(name.endswith("private.env") for name in archive.getnames())
        name = next(name for name in archive.getnames() if name.endswith("RELEASE.json"))
        manifest = json.load(archive.extractfile(name))
        assert manifest["files"]["resources_servers/spreadsheet/app.py"] == data.digest(app)
