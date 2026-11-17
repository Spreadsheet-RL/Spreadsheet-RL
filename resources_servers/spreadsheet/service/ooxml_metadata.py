"""Preserve valid input metadata when a calculation-only LO export corrupts it.

Only filters, validation rules and merge ranges are eligible. A malformed input,
changed table topology, pivot caches and chart data are never guessed or erased.
No worksheet cells, formulas, cached values or styles are patched.
"""

import io
import posixpath
from copy import deepcopy
from xml.etree import ElementTree as ET
from xml.parsers import expat
from zipfile import ZipFile


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def sheet_parts(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.attrib["Id"]: r.attrib["Target"] for r in relations}
    return {
        s.attrib["name"]: (
            targets[s.attrib[REL]].lstrip("/")
            if targets[s.attrib[REL]].startswith("/")
            else posixpath.normpath("xl/" + targets[s.attrib[REL]])
        )
        for s in workbook.find(NS + "sheets")
    }


def valid(element):
    from openpyxl.xml.functions import fromstring
    from openpyxl.worksheet.datavalidation import DataValidationList
    from openpyxl.worksheet.filters import AutoFilter
    from openpyxl.worksheet.merge import MergeCells

    classes = {
        "autoFilter": AutoFilter,
        "dataValidations": DataValidationList,
        "mergeCells": MergeCells,
    }
    try:
        classes[element.tag.removeprefix(NS)].from_tree(
            fromstring(ET.tostring(element))
        )
        return True
    except (ValueError, TypeError, KeyError):
        return False


def capture(path):
    """Remember only schema-valid metadata from the workspace being calculated."""
    result = {"sheets": {}, "tables": {}}
    with ZipFile(path) as archive:
        for title, part in sheet_parts(archive).items():
            root = ET.fromstring(archive.read(part))
            result["sheets"][title] = {
                element.tag: deepcopy(element)
                for element in root
                if element.tag
                in {NS + "autoFilter", NS + "dataValidations", NS + "mergeCells"}
                and valid(element)
            }
        for part in archive.namelist():
            if part.startswith("xl/tables/") and part.endswith(".xml"):
                table = ET.fromstring(archive.read(part))
                element = table.find(NS + "autoFilter")
                if element is not None and valid(element):
                    key = (table.attrib.get("displayName"), table.attrib.get("ref"))
                    result["tables"][key] = deepcopy(element)
    return result


def replace_subtrees(content, replacements):
    """Patch byte spans only; retain namespace declarations and all cell XML."""
    spans, stack = [], []
    parser = expat.ParserCreate(namespace_separator="|")

    def start(name, attributes):
        stack.append((name, parser.CurrentByteIndex))

    def end(name):
        _, first = stack.pop()
        tag = "{" + name.replace("|", "}", 1) if "|" in name else name
        if len(stack) == 1 and tag in replacements:
            last = parser.CurrentByteIndex
            if content[last : last + 2] == b"</":
                last = content.index(b">", last) + 1
            spans.append((first, last, ET.tostring(replacements[tag])))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(content, True)
    for first, last, replacement in sorted(spans, reverse=True):
        content = content[:first] + replacement + content[last:]
    return content


def restore(path, before):
    """Repair only an invalid exported subtree with its valid input counterpart."""
    replacements, repairs = {}, []
    with ZipFile(path) as archive:
        for title, part in sheet_parts(archive).items():
            content = archive.read(part)
            tree = ET.fromstring(content)
            changed = {}
            for element in tree:
                saved = before["sheets"].get(title, {}).get(element.tag)
                if saved is not None and not valid(element):
                    changed[element.tag] = saved
                    repairs.append({"part": part, "kind": element.tag.removeprefix(NS)})
            if changed:
                replacements[part] = replace_subtrees(content, changed)
        for part in archive.namelist():
            if part.startswith("xl/tables/") and part.endswith(".xml"):
                content = archive.read(part)
                tree = ET.fromstring(content)
                key = (tree.attrib.get("displayName"), tree.attrib.get("ref"))
                element = tree.find(NS + "autoFilter")
                saved = before["tables"].get(key)
                if element is not None and saved is not None and not valid(element):
                    replacements[part] = replace_subtrees(
                        content, {NS + "autoFilter": saved}
                    )
                    repairs.append({"part": part, "kind": "tableAutoFilter"})
        if replacements:
            buffer = io.BytesIO()
            with ZipFile(buffer, "w") as target:
                for entry in archive.infolist():
                    target.writestr(
                        entry, replacements.get(entry.filename, archive.read(entry))
                    )
            path.write_bytes(buffer.getvalue())
    return repairs
