import json
from pathlib import Path

import pytest

from resources_servers.spreadsheet.packaging import convert_training_data as converter
from resources_servers.spreadsheet.schemas import tool_schemas


def row(task_id="task-a"):
    return {
        "task_id": task_id,
        "dataset_revision": "pinned-revision",
        "responses_create_params": {
            "input": [{"role": "user", "content": "Edit data.xlsx"}],
            "tools": tool_schemas(),
            "parallel_tool_calls": False,
        },
    }


def test_conversion_adds_agent_routing_and_preserves_the_entire_request():
    original = row()
    result = converter.convert_row(original, "spreadsheet_agent")
    assert result == dict(original, agent_ref={"name": "spreadsheet_agent", "type": "responses_api_agents"})
    assert "agent_ref" not in original
    # Validate against the actual Gym request schema, without model inference.
    from nemo_gym.openai_utils import NeMoGymResponseCreateParamsNonStreaming

    parsed = NeMoGymResponseCreateParamsNonStreaming.model_validate(result["responses_create_params"])
    assert len(parsed.tools) == 12


@pytest.mark.parametrize(
    "change",
    [
        {"rollout_id": "static-rollout"},
        {"session_id": "static-session"},
        {"agent_ref": {"name": "different_agent"}},
        {"task_id": ""},
    ],
)
def test_incompatible_rows_fail_instead_of_reusing_sessions_or_changing_agents(change):
    with pytest.raises(ValueError):
        converter.convert_row(dict(row(), **change), "spreadsheet_agent")


def test_train_and_test_conversion_is_repeatable_and_leaves_inputs_unchanged(tmp_path):
    source = tmp_path / "qualified"
    source.mkdir()
    originals = {}
    for split, ids in (("train", ["a", "b"]), ("test", ["c"])):
        path = source / f"{split}.qualified.jsonl"
        path.write_text("".join(json.dumps(row(task_id)) + "\n" for task_id in ids))
        originals[path] = path.read_bytes()
    output = tmp_path / "nemo_gym"
    receipt = converter.convert(source, output, "spreadsheet_agent")
    assert receipt["rows"] == {"train": 2, "test": 1}
    assert converter.convert(source, output, "spreadsheet_agent") == receipt
    assert all(path.read_bytes() == contents for path, contents in originals.items())
    for split in ("train", "test"):
        assert all(
            json.loads(line)["agent_ref"]["name"] == "spreadsheet_agent"
            for line in (output / f"{split}.jsonl").read_text().splitlines()
        )
    (output / "train.jsonl").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        converter.convert(source, output, "spreadsheet_agent")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_yaml_patch_changes_only_the_train_path_and_creates_a_private_backup(tmp_path, newline):
    config = tmp_path / "qwen_test.yaml"
    text = newline.join(
        [
            'defaults: "/existing/base.yaml"',
            "grpo:",
            "  num_prompts_per_step: 128",
            "data:",
            "  shuffle: true",
            "  train:",
            "    - data_path: /old/train.jsonl  # retained comment",
            "  validation: null",
            "env:",
            "  should_use_nemo_gym: true",
            "",
        ]
    )
    config.write_bytes(text.encode())
    config.chmod(0o640)
    target = Path("/new/nemo_gym/train.jsonl")
    backup = Path(converter.patch_train_path(config, target))
    expected = text.replace("/old/train.jsonl", json.dumps(str(target)))
    assert config.read_bytes() == expected.encode()
    assert backup.read_bytes() == text.encode()
    assert not backup.stat().st_mode & 0o077
    assert config.stat().st_mode & 0o777 == 0o640
    assert converter.patch_train_path(config, target) is None


def test_yaml_with_multiple_training_datasets_is_not_changed_implicitly(tmp_path):
    config = tmp_path / "qwen_test.yaml"
    original = "data:\n  train:\n    - data_path: /one\n    - data_path: /two\n  validation: null\n"
    config.write_text(original)
    with pytest.raises(ValueError, match="ambiguous"):
        converter.patch_train_path(config, Path("/new"))
    assert config.read_text() == original
    assert not list(tmp_path.glob("*.bak"))


def test_yaml_symlink_is_not_replaced_or_followed_implicitly(tmp_path):
    target = tmp_path / "shared-template.yaml"
    target.write_text("data:\n  train:\n    - data_path: /original\n")
    link = tmp_path / "qwen_test.yaml"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        converter.patch_train_path(link, Path("/new"))
    assert link.is_symlink()
    assert "/original" in target.read_text()


def test_future_parquet_preparation_includes_agent_routing(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    from resources_servers.spreadsheet.prepare import prepare

    task = tmp_path / "workbooks/task"
    task.mkdir(parents=True)
    for name in ("output.xlsx", "target.xlsx"):
        (task / name).write_bytes(b"fixture-file-hashed-by-preparation")
    parquet = tmp_path / "train_hermes.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "extra_info": {"id": "task-a", "answer_position": "Sheet1!A1"},
                    "reward_model": {"ground_truth": "task"},
                    "prompt": [{"role": "user", "content": "Edit data.xlsx"}],
                }
            ]
        ),
        parquet,
    )
    destination = tmp_path / "prepared"
    assert prepare([parquet], tmp_path / "workbooks", destination, "pinned-revision") == 1
    output = json.loads((destination / "train_hermes.jsonl").read_text())
    assert output["agent_ref"] == {"name": "spreadsheet_agent", "type": "responses_api_agents"}
