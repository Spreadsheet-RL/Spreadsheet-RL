from pathlib import Path
import venv

from resources_servers.spreadsheet.packaging.inspect_relocated_runtime import file_report, venv_report


def test_moved_venv_python_and_activation_can_disagree_without_rewriting_either(tmp_path):
    old = tmp_path / "before"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(old)
    activation_before = (old / "bin/activate").read_bytes()
    moved = tmp_path / "after"
    old.rename(moved)
    report = venv_report(moved)
    assert report["returncode"] == 0
    assert report["python_prefix_matches_location"] is True
    assert report["activation_matches_location"] is False
    assert Path(report["activation_virtual_env"]) == old
    assert (moved / "bin/activate").read_bytes() == activation_before
    assert not old.exists()


def test_missing_venv_interpreter_is_reported_without_executing_other_python(tmp_path):
    report = venv_report(tmp_path / "missing")
    assert report["python_exists"] is False
    assert "python_probe" not in report


def test_report_selects_path_and_layout_fields_without_dumping_credentials(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "model_name: /workspace/models/example\napi_key: fixture-private-key\npolicy:\n  megatron_cfg:\n    tensor_model_parallel_size: 4\n"
    )
    report = file_report(config, selected_keys=True)
    assert "fixture-private-key" not in str(report)
    assert {row["field"] for row in report["explicit_yaml_fields_only"]} == {
        "model_name",
        "tensor_model_parallel_size",
    }
