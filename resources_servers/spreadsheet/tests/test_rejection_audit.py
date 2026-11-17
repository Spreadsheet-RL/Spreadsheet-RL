import hashlib
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from resources_servers.spreadsheet.packaging.audit_rejections import exception_chain


def test_private_exception_report_preserves_stages_without_unbounded_tracebacks():
    cause = ValueError("inner" + "x" * 1000)
    error = RuntimeError("outer")
    error.__cause__ = cause
    result = exception_chain(error)
    assert [part["type"] for part in result] == ["RuntimeError", "ValueError"]
    assert len(result[1]["message"]) == 500


def test_audit_recovers_metadata_limit_with_strict_reward_and_native_roundtrip(
    tmp_path,
):
    openpyxl = pytest.importorskip("openpyxl")
    from resources_servers.spreadsheet.packaging.audit_rejections import audit_task
    from resources_servers.spreadsheet.service.workbook import compare, split_ranges

    path = tmp_path / "target.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Result"
    wb.active["A1"] = 7
    wb.save(path)
    wb.close()
    source = path.read_bytes()
    archive_path = tmp_path / "inputs.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("task/target.xlsx", source)
        archive.writestr("task/output.xlsx", source)

    def old_compare(oracle, actual, address):
        split_ranges(address)
        return compare(oracle, actual, address)

    engine = SimpleNamespace(recalculate=lambda path: None)
    task = {
        "task_id": "task",
        "thread": "task",
        "split": "train",
        "dataset_revision": "pinned",
        "answer_position": ",".join(f"Result!A{i}" for i in range(1, 25)),
    }
    with ZipFile(archive_path) as archive:
        record = audit_task(
            task, archive, SimpleNamespace(compare=old_compare), engine, tmp_path
        )
    assert record["verdict"] == "recovered_strict"
    assert record["native_roundtrip"]["passed"]
    assert len(record["attempts"]) == 2
    assert record["oracle_sha256"] == hashlib.sha256(source).hexdigest()
    assert record["seed_sha256"] == record["oracle_sha256"]
    assert path.read_bytes() == source


def test_audit_marks_new_reader_regressions_instead_of_promoting_them(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from resources_servers.spreadsheet.packaging.audit_rejections import audit_task

    wb = openpyxl.Workbook()
    wb.active["A1"] = 7
    original = tmp_path / "target.xlsx"
    wb.save(original)
    wb.close()
    archive_path = tmp_path / "inputs.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("task/target.xlsx", original.read_bytes())

    def recalculate(path):
        book = openpyxl.load_workbook(path)
        book.active["A1"] = 8
        book.save(path)
        book.close()

    task = {
        "task_id": "task",
        "thread": "task",
        "split": "test",
        "answer_position": "Sheet!A1",
    }
    baseline = SimpleNamespace(compare=lambda *args: {"reward": 1})
    with ZipFile(archive_path) as archive:
        record = audit_task(
            task, archive, baseline, SimpleNamespace(recalculate=recalculate), tmp_path
        )
    assert record["verdict"] == "strict_regression"
    assert record["baseline_qualified"] and not record["strict_qualified"]


def test_negative_control_rejects_changed_answer_and_seed_roundtrip_preserves_cells(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    from resources_servers.spreadsheet.packaging.recovery_cohort import negative_control, seed_roundtrip

    oracle, actual = tmp_path / 'oracle.xlsx', tmp_path / 'actual.xlsx'
    wb = openpyxl.Workbook()
    wb.active.title = 'Result'
    wb.active['B2'] = 42
    wb.save(oracle)
    wb.save(actual)
    wb.close()
    before = oracle.read_bytes()
    result = negative_control(oracle, actual, 'Result!B2:C4', tmp_path / 'wrong.xlsx')
    assert result == {'passed': True, 'reward': 0}
    assert seed_roundtrip(actual, SimpleNamespace(recalculate=lambda path: None))['passed']
    assert oracle.read_bytes() == before


def test_seed_digest_uses_array_formula_content_not_object_address(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    from openpyxl.worksheet.formula import ArrayFormula
    from resources_servers.spreadsheet.packaging.recovery_cohort import content_digest, seed_roundtrip

    path = tmp_path / 'array.xlsx'
    book = openpyxl.Workbook()
    book.active['A1'], book.active['A2'] = 1, 2
    book.active['B1'] = ArrayFormula(ref='B1:B2', text='=A1:A2*2')
    book.save(path)
    book.close()
    original = content_digest(path)
    assert content_digest(path) == original
    assert seed_roundtrip(path, SimpleNamespace(recalculate=lambda path: None))['passed']
    assert content_digest(path) == original
    book = openpyxl.load_workbook(path)
    book.active['B1'] = ArrayFormula(ref='B1:B2', text='=A1:A2*3')
    book.save(path)
    book.close()
    assert content_digest(path) != original
