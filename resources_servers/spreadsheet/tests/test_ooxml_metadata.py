"""Lossless metadata recovery from the same pre-calculation workspace."""

import io
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

openpyxl = pytest.importorskip("openpyxl")

from resources_servers.spreadsheet.service.ooxml_metadata import capture, restore  # noqa: E402

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def replace(path, name, transform):
    buffer = io.BytesIO()
    with ZipFile(path) as source, ZipFile(buffer, "w") as target:
        for entry in source.infolist():
            content = source.read(entry)
            target.writestr(
                entry, transform(content) if entry.filename == name else content
            )
    path.write_bytes(buffer.getvalue())


def sample(path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet["A1"], sheet["A2"] = "Heading", "=1+2"
    sheet.auto_filter.ref = "A1:A2"
    sheet.auto_filter.add_filter_column(0, ["Heading"])
    from openpyxl.worksheet.datavalidation import DataValidation

    rule = DataValidation(type="whole", operator="between", formula1=1, formula2=9)
    sheet.add_data_validation(rule)
    rule.add("B2:B9")
    sheet.merge_cells("C1:D1")
    book.save(path)
    book.close()


def test_valid_export_is_byte_identical(tmp_path):
    path = tmp_path / "work.xlsx"
    sample(path)
    before = path.read_bytes()
    assert restore(path, capture(path)) == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "tag,attribute,value",
    [
        ("autoFilter", "ref", "INVALID"),
        ("dataValidation", "sqref", "B:B"),
        ("mergeCell", "ref", "C:D"),
    ],
)
def test_only_invalid_export_metadata_replaced_no_cell_or_other_part_changes(
    tmp_path, tag, attribute, value
):
    path = tmp_path / "work.xlsx"
    sample(path)
    metadata = capture(path)

    def corrupt(content):
        tree = ET.fromstring(content)
        element = next(e for e in tree.iter() if e.tag == NS + tag)
        element.set(attribute, value)
        # Preserve a legitimate new result made by the recalculation engine.
        cell = next(e for e in tree.iter(NS + "c") if e.attrib["r"] == "A2")
        cell.find(NS + "v").text = "3"
        return ET.tostring(tree)

    replace(path, "xl/worksheets/sheet1.xml", corrupt)
    with ZipFile(path) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
        cells_before = ET.tostring(
            ET.fromstring(parts["xl/worksheets/sheet1.xml"]).find(NS + "sheetData")
        )
    assert len(restore(path, metadata)) == 1
    with ZipFile(path) as archive:
        assert all(
            archive.read(n) == content
            for n, content in parts.items()
            if n != "xl/worksheets/sheet1.xml"
        )
        cells_after = ET.tostring(
            ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).find(
                NS + "sheetData"
            )
        )
    assert cells_before == cells_after
    wb = openpyxl.load_workbook(path)
    assert wb.active["A2"].value == "=1+2"
    assert wb.active.auto_filter.ref == "A1:A2"
    wb.close()


def test_unknown_invalid_filter_operator_not_invented_without_valid_input(tmp_path):
    path = tmp_path / "work.xlsx"
    sample(path)

    def corrupt(content):
        tree = ET.fromstring(content)
        auto = tree.find(NS + "autoFilter")
        auto.clear()
        auto.set("ref", "A1:A2")
        column = ET.SubElement(auto, NS + "filterColumn", colId="0")
        filters = ET.SubElement(column, NS + "customFilters")
        ET.SubElement(filters, NS + "customFilter", operator="**none**", val="")
        return ET.tostring(tree)

    replace(path, "xl/worksheets/sheet1.xml", corrupt)
    metadata = capture(path)
    before = path.read_bytes()
    assert restore(path, metadata) == []
    assert path.read_bytes() == before
