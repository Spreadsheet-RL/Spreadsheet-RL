"""Offline orchestration tests; real LO qualification runs on the service node."""

import json
import stat
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from resources_servers.spreadsheet.packaging import prepare_dataset as node


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a\\escape"])
def test_extraction_rejects_unsafe_archive_paths(tmp_path, name):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(name, b"not extracted")
    with pytest.raises(ValueError, match="Unsafe archive path"):
        node.extract_archive(archive, tmp_path / "workbooks", node.digest(archive))
    assert not (tmp_path / "workbooks").exists()


def test_extraction_rejects_symlinks(tmp_path):
    archive = tmp_path / "archive.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(info, "../escape")
    with pytest.raises(ValueError, match="symlinks"):
        node.extract_archive(archive, tmp_path / "workbooks", node.digest(archive))


def test_extraction_crc_failure_does_not_publish_partial_dataset(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as stream:
        stream.writestr("task/output.xlsx", b"UNIQUE_WORKBOOK_BYTES")
    archive.write_bytes(archive.read_bytes().replace(b"UNIQUE_WORKBOOK_BYTES", b"BROKEN_WORKBOOK_BYTES"))
    with pytest.raises(zipfile.BadZipFile):
        node.extract_archive(archive, tmp_path / "workbooks", node.digest(archive))
    assert not (tmp_path / "workbooks").exists()


def test_extraction_resume_and_optional_wrapper(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("spreadsheets/train/task/output.xlsx", b"original seed")
        stream.writestr("spreadsheets/.DS_Store", b"junk")
    destination = tmp_path / "workbooks"
    sha = node.digest(archive)
    node.extract_archive(archive, destination, sha)
    node.extract_archive(archive, destination, sha)
    root = node.find_data_root(destination, ["train/task"])
    assert (root / "train/task/output.xlsx").read_bytes() == b"original seed"
    assert not (root / ".DS_Store").exists()
    with pytest.raises(ValueError, match="different receipt"):
        node.extract_archive(archive, destination, "different archive")


def test_interrupted_qualification_resumes_only_completed_batches(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("".join(json.dumps({"task_id": str(i)}) + "\n" for i in range(6)))
    engine = SimpleNamespace(version="test-LO")
    attempted = []
    fail = True

    def qualify(source, _data_root, output, report, _engine):
        nonlocal fail
        items = [json.loads(line) for line in source.read_text().splitlines()]
        ids = [item["task_id"] for item in items]
        attempted.append(ids)
        if fail and ids == ["2", "3"]:
            raise RuntimeError("interrupted before completion")
        selected = [dict(item, libreoffice_qualified=True) for item in items if int(item["task_id"]) % 2 == 0]
        output.write_text("".join(json.dumps(row) + "\n" for row in selected))
        node.write_json(
            report,
            {
                "source_manifest_sha256": node.digest(source),
                "engine": engine.version,
                "total": len(items),
                "qualified": len(selected),
                "tasks": [{"task_id": item["task_id"], "qualified": int(item["task_id"]) % 2 == 0} for item in items],
            },
        )

    module = SimpleNamespace(qualify=qualify)
    with patch.dict(sys.modules, {"resources_servers.spreadsheet.qualify": module}):
        with pytest.raises(RuntimeError, match="interrupted"):
            node.qualify_batches(manifest, tmp_path, tmp_path / "batches", engine, 2, "helper")
        assert (tmp_path / "batches/000000/checkpoint.json").is_file()
        assert not (tmp_path / "batches/000002").exists()
        fail = False
        selected, report = node.qualify_batches(manifest, tmp_path, tmp_path / "batches", engine, 2, "helper")
        assert attempted == [["0", "1"], ["2", "3"], ["2", "3"], ["4", "5"]]
        assert [row["task_id"] for row in selected] == ["0", "2", "4"]
        assert len(report) == 6
        engine.version = "different-LO"
        with pytest.raises(ValueError, match="contract changed"):
            node.qualify_batches(manifest, tmp_path, tmp_path / "batches", engine, 2, "helper")


def test_published_training_and_test_rows_match_qualified_catalog(tmp_path):
    for split, ids in (("train", ["a", "b"]), ("test", ["c", "d"])):
        (tmp_path / f"{split}_hermes.jsonl").write_text(
            "".join(json.dumps({"task_id": task, "dataset_revision": node.REVISION}) + "\n" for task in ids)
        )
    selected = [{"task_id": task, "libreoffice_qualified": True} for task in ("b", "c")]
    diagnostics = [
        {"task_id": task, "qualified": task in ("b", "c"), **({} if task in ("b", "c") else {"reason": "mismatch"})}
        for task in ("a", "b", "c", "d")
    ]
    result = node.publish_results(tmp_path, selected, diagnostics, {"engine": "test-LO"})
    for split, expected in (("train", ["b"]), ("test", ["c"])):
        assert [
            json.loads(line)["task_id"] for line in (result / f"{split}.qualified.jsonl").read_text().splitlines()
        ] == expected
    assert node.read_json(result / "compatibility.json")["reasons"] == {"qualified": 2, "mismatch": 2}
    assert node.publish_results(tmp_path, selected, diagnostics, {"engine": "test-LO"}) == result
    with pytest.raises(ValueError, match="different contract"):
        node.publish_results(tmp_path, selected, diagnostics, {"engine": "other"})
