"""Bounded workbook operations, independent of Gym and verl.

The API intentionally documents openpyxl structural-edit semantics rather than
claiming Excel's automatic dependency repair. Recalculation is a separate engine.
"""

import math
import re
from copy import copy
from datetime import date, datetime, time
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .errors import ToolError


MAX_CELLS = 100_000
MAX_RETURN = 400
MAX_ANSWER_RANGES = 4096
MAX_ANSWER_CELLS = 1_000_000


def check_archive(path: Path, max_bytes: int = 100 * 1024 * 1024) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ToolError("Workbook is missing, symlinked, or exceeds the size limit")
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 20_000 or sum(i.file_size for i in infos) > 512 * 1024 * 1024:
                raise ToolError("Workbook decompressed size exceeds the limit")
            if "xl/workbook.xml" not in archive.namelist():
                raise ToolError("File is not an XLSX workbook")
    except BadZipFile:
        raise ToolError("Invalid XLSX archive") from None


def load(path: Path, *, values: bool = False, read_only: bool = False):
    from openpyxl import load_workbook

    check_archive(path)
    try:
        return load_workbook(path, data_only=values, read_only=read_only, keep_links=False)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ToolError("Workbook cannot be opened") from exc


def json_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def split_ranges(text: str, *, max_ranges: int = 20) -> list[str]:
    """Split commas outside a leading quoted sheet name (including doubled quotes)."""
    parts, current, quoted, at_start = [], [], False, True
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'" and (quoted or at_start):
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                current.extend([char, char])
                index += 2
                continue
            quoted = not quoted
        if char == "," and not quoted:
            parts.append("".join(current).strip())
            current, at_start = [], True
        else:
            current.append(char)
            if not char.isspace():
                at_start = False
        index += 1
    if quoted:
        raise ToolError("Unclosed sheet-name quote")
    parts.append("".join(current).strip())
    if len(parts) > max_ranges or any(not part for part in parts):
        raise ToolError(f"Expected 1–{max_ranges} A1 ranges")
    return parts


def resolve(wb, address: str, sheet_name: str | None = None):
    from openpyxl.utils.cell import range_boundaries

    if "!" in address:
        prefix, address = address.rsplit("!", 1)
        if prefix in wb.sheetnames:
            sheet_name = prefix
        else:
            # Dataset answer_position sometimes contains a dangling closing quote.
            sheet_name = prefix.strip("'").replace("''", "'")
    if not re.fullmatch(r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?", address):
        raise ToolError("Use a finite A1 cell or rectangle")
    sheet_name = sheet_name if sheet_name is not None else wb.sheetnames[0]
    if sheet_name not in wb.sheetnames:
        raise ToolError("Worksheet does not exist")
    bounds = range_boundaries(address.replace("$", ""))
    left, top, right, bottom = bounds
    if not (1 <= left <= right <= 16384 and 1 <= top <= bottom <= 1048576):
        raise ToolError("Invalid or reversed Excel range")
    if (right - left + 1) * (bottom - top + 1) > MAX_CELLS:
        raise ToolError(f"At most {MAX_CELLS} cells per range; split the request")
    return wb[sheet_name], bounds


def records(path: Path, addresses: list[str], sheet_name=None, *, summary=False, details=False):
    wb, cached = load(path), load(path, values=True)
    try:
        results = []
        for address in addresses:
            ws, (left, top, right, bottom) = resolve(wb, address, sheet_name)
            values = cached[ws.title]
            cells, counts, omitted = [], {"non_empty": 0, "empty": 0, "formula": 0, "error": 0}, None
            for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                for cell in row:
                    counts["empty" if cell.value is None else "non_empty"] += 1
                    counts["formula"] += cell.data_type == "f"
                    counts["error"] += values[cell.coordinate].data_type == "e"
                    if not summary and len(cells) < MAX_RETURN:
                        item = {
                            "address": cell.coordinate,
                            "value": json_value(values[cell.coordinate].value),
                            "formula": cell.value if cell.data_type == "f" else None,
                        }
                        if details:
                            item.update(
                                number_format=cell.number_format,
                                bold=cell.font.bold,
                                fill_color=cell.fill.fgColor.rgb if cell.fill.fgColor.type == "rgb" else None,
                            )
                        cells.append(item)
                    elif not summary and omitted is None:
                        omitted = cell.coordinate
            result = {"sheet_name": ws.title, "range": address}
            if summary:
                result.update(counts)
            else:
                result.update(cells=cells, truncated=omitted is not None, omitted_from=omitted)
            results.append(result)
        return {"ranges": results}
    finally:
        wb.close()
        cached.close()


def _color(text: str):
    colors = {
        "red": "FF0000",
        "green": "008000",
        "blue": "0000FF",
        "white": "FFFFFF",
        "black": "000000",
        "yellow": "FFFF00",
    }
    value = colors.get(text.lower(), text.lstrip("#"))
    if not re.fullmatch(r"(?:[A-Fa-f0-9]{6}|[A-Fa-f0-9]{8})", value):
        raise ToolError("Use a hex color or red/green/blue/white/black/yellow")
    return value if len(value) == 8 else "FF" + value


def execute(path: Path, name: str, args: dict):
    from openpyxl.cell.cell import MergedCell
    from openpyxl.formula.tokenizer import TokenizerError
    from openpyxl.formula.translate import Translator, TranslatorError
    from openpyxl.styles import PatternFill, Side
    from openpyxl.utils.cell import column_index_from_string, get_column_letter

    if name == "inspect_range":
        return records(
            path,
            split_ranges(args["range"]),
            args.get("sheet_name"),
            summary=args["mode"] == "summary",
            details=args["include_details"],
        )
    if name == "recalculate_and_read":
        return records(path, args["cell_ranges"])
    wb = load(path)
    try:
        if name == "list_sheets":
            return {
                "sheets": [
                    {"name": ws.title, "index": index, "state": ws.sheet_state, "dimensions": ws.calculate_dimension()}
                    for index, ws in enumerate(wb)
                ]
            }
        if name == "find_cells":
            query = args["query"] if args["case_sensitive"] else args["query"].casefold()
            matches = []
            addresses = (
                [args["range"]]
                if args["range"]
                else [
                    f"'{ws.title.replace(chr(39), chr(39) * 2)}'!{ws.calculate_dimension()}"
                    for ws in wb
                    if args.get("sheet_name") in (None, ws.title)
                ]
            )
            cached = load(path, values=True)
            try:
                for address in addresses:
                    ws, (left, top, right, bottom) = resolve(wb, address, args.get("sheet_name"))
                    for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                        for cell in row:
                            candidates = []
                            if args["search_in"] in ("values", "both"):
                                candidates.append(cached[ws.title][cell.coordinate].value)
                            if args["search_in"] in ("formulas", "both") and cell.data_type == "f":
                                candidates.append(cell.value)
                            texts = [
                                str(v) if args["case_sensitive"] else str(v).casefold()
                                for v in candidates
                                if v is not None
                            ]
                            matched = any(
                                {"contains": query in text, "equals": query == text, "prefix": text.startswith(query)}[
                                    args["match"]
                                ]
                                for text in texts
                            )
                            if matched:
                                record = {"sheet_name": ws.title, "address": cell.coordinate}
                                if args["include_values"]:
                                    record["value"] = json_value(cell.value)
                                matches.append(record)
                                if args["return"] == "first" or len(matches) >= args["max_results"]:
                                    return {"matches": matches, "limit_reached": True}
                return {"matches": matches, "limit_reached": False}
            finally:
                cached.close()
        if name == "manage_sheet":
            action, title = args["action"], args["sheet_name"]
            if action == "create":
                if title in wb.sheetnames or len(wb.sheetnames) >= 200:
                    raise ToolError("Sheet already exists or sheet limit reached")
                wb.create_sheet(title, args.get("index"))
            else:
                if title not in wb.sheetnames:
                    raise ToolError("Worksheet does not exist")
                ws = wb[title]
                if (
                    action in ("delete", "hide")
                    and ws.sheet_state == "visible"
                    and sum(s.sheet_state == "visible" for s in wb) <= 1
                ):
                    raise ToolError("Cannot remove the last visible sheet")
                if action == "rename":
                    if not args.get("new_name") or args["new_name"] in wb.sheetnames:
                        raise ToolError("A unique new_name is required")
                    ws.title = args["new_name"]
                elif action == "delete":
                    wb.remove(ws)
                elif action == "copy":
                    if len(wb.sheetnames) >= 200 or args.get("new_name") in wb.sheetnames:
                        raise ToolError("Sheet name conflict or sheet limit reached")
                    ws = wb.copy_worksheet(ws)
                    if args.get("new_name"):
                        ws.title = args["new_name"]
                elif action in ("hide", "unhide"):
                    ws.sheet_state = "hidden" if action == "hide" else "visible"
                if action == "move" or action == "copy" and args.get("index") is not None:
                    if args.get("index") is None or args["index"] >= len(wb.sheetnames):
                        raise ToolError("index is required and must be within the sheet list")
                    wb.move_sheet(ws, offset=args["index"] - wb.index(ws))
            result = {"sheets": wb.sheetnames}
        elif name in ("delete_rows", "delete_columns"):
            is_row = name == "delete_rows"
            pieces = args["rows" if is_row else "columns"]
            title, intervals = args.get("sheet_name"), []
            for piece in pieces:
                if "!" in piece:
                    prefix, piece = piece.rsplit("!", 1)
                    candidate = prefix.strip("'").replace("''", "'")
                    if title is not None and candidate != title:
                        raise ToolError("One sheet per deletion call")
                    title = candidate
                tokens = piece.split(":")
                if len(tokens) > 2 or any(
                    not re.fullmatch(r"[1-9][0-9]*" if is_row else r"[A-Z]{1,3}", t) for t in tokens
                ):
                    raise ToolError("Invalid row/column interval")
                numbers = [int(t) if is_row else column_index_from_string(t) for t in tokens]
                first, last = numbers[0], numbers[-1]
                if not 1 <= first <= last <= (1048576 if is_row else 16384):
                    raise ToolError("Invalid row/column interval")
                intervals.append((first, last))
            intervals.sort()
            if (
                any(a[1] >= b[0] for a, b in zip(intervals, intervals[1:]))
                or sum(b - a + 1 for a, b in intervals) > 10000
            ):
                raise ToolError("Overlapping intervals or deletion limit exceeded")
            title = title or wb.sheetnames[0]
            if title not in wb.sheetnames:
                raise ToolError("Worksheet does not exist")
            ws = wb[title]
            for first, last in reversed(intervals):
                (ws.delete_rows if is_row else ws.delete_cols)(first, last - first + 1)
            result = {"deleted": sum(b - a + 1 for a, b in intervals)}
        else:
            address = args.get("range", args.get("start_cell"))
            if name == "write_range" and args.get("sheet_name") and args["sheet_name"] not in wb.sheetnames:
                wb.create_sheet(args["sheet_name"])
            ws, (left, top, right, bottom) = resolve(wb, address, args.get("sheet_name"))
            if name == "fill_formula":
                right = column_index_from_string(args["end_col"]) if args.get("end_col") else left
                bottom = args.get("end_row") or top
                ws, _ = resolve(wb, f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}", ws.title)
                origin = f"{get_column_letter(left)}{top}"
                for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                    for cell in row:
                        if not isinstance(cell, MergedCell):
                            cell.value = Translator(args["formula_template"], origin=origin).translate_formula(
                                cell.coordinate
                            )
            elif name == "write_range":
                data = args["data"]
                matrix = data if isinstance(data, list) else [[data]]
                if not matrix:
                    raise ToolError("data cannot be empty")
                if not isinstance(matrix[0], list):
                    matrix = [matrix]
                if any(not isinstance(row, list) or len(row) != len(matrix[0]) for row in matrix) or not matrix[0]:
                    raise ToolError("Expected a rectangular value matrix")
                height, width = len(matrix), len(matrix[0])
                if left == right and top == bottom:
                    right, bottom = left + width - 1, top + height - 1
                    resolve(wb, f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}", ws.title)
                if height not in (1, bottom - top + 1) or width not in (1, right - left + 1):
                    raise ToolError("data shape does not match the target rectangle")
                for r in range(top, bottom + 1):
                    for c in range(left, right + 1):
                        value = matrix[(r - top) % height][(c - left) % width]
                        if value is None:
                            continue
                        if (
                            type(value) not in (str, int, float, bool)
                            or isinstance(value, float)
                            and not math.isfinite(value)
                        ):
                            raise ToolError("Only finite numbers, strings, booleans, or null are allowed")
                        if isinstance(value, str) and (value.startswith("=") or len(value) > 8192):
                            raise ToolError("Formula strings or oversized values are not allowed in write_range")
                        ws.cell(r, c).value = value
            elif name in ("clear_range", "format_range"):
                for row in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                    for cell in row:
                        if isinstance(cell, MergedCell):
                            continue
                        if name == "clear_range":
                            cell.value = None
                            continue
                        font, alignment, border = copy(cell.font), copy(cell.alignment), copy(cell.border)
                        for field in ("bold", "italic"):
                            if args.get(field) is not None:
                                setattr(font, field, args[field])
                        if args.get("underline") is not None:
                            font.underline = "single" if args["underline"] else None
                        if args.get("font_color"):
                            font.color = _color(args["font_color"])
                        if args.get("fill_color"):
                            cell.fill = PatternFill("solid", fgColor=_color(args["fill_color"]))
                        for field, target in (
                            ("horizontal_alignment", "horizontal"),
                            ("vertical_alignment", "vertical"),
                            ("wrap_text", "wrap_text"),
                            ("shrink_to_fit", "shrink_to_fit"),
                            ("text_rotation", "text_rotation"),
                        ):
                            if args.get(field) is not None:
                                setattr(alignment, target, args[field])
                        if args.get("border_style") or args.get("border_color"):
                            side = Side(
                                style=args.get("border_style") or "thin",
                                color=_color(args.get("border_color") or "black"),
                            )
                            border.left = border.right = border.top = border.bottom = side
                        cell.font, cell.alignment, cell.border = font, alignment, border
                        if args.get("number_format") is not None:
                            cell.number_format = args["number_format"]
                if name == "format_range":
                    if args.get("row_height") is not None:
                        for row in range(top, bottom + 1):
                            ws.row_dimensions[row].height = args["row_height"]
                    if args.get("column_width") is not None:
                        for col in range(left, right + 1):
                            ws.column_dimensions[get_column_letter(col)].width = args["column_width"]
            else:
                raise ToolError("Unknown native tool")
            result = {
                "sheet_name": ws.title,
                "changed_range": f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}",
            }
        wb.save(path)
        return result
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, TokenizerError, TranslatorError) as exc:
        if isinstance(exc, ToolError):
            raise
        raise ToolError(str(exc)[:300]) from exc
    finally:
        wb.close()


def normalize(value, epoch=None):
    # Match the current upstream evaluator's date/time normalization, which is
    # more precise than the date/time shorthand in the paper's prose.
    if isinstance(value, datetime):
        value = value.replace(tzinfo=None)
        return round((value - datetime(1899, 12, 30)).total_seconds() / 86400, 8)
    if isinstance(value, date):
        return round((datetime.combine(value, time()) - datetime(1899, 12, 30)).total_seconds() / 86400, 8)
    if isinstance(value, time):
        return round((value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000) / 86400, 8)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else str(value)
    except (TypeError, ValueError, OverflowError):
        return value


def answer_regions(workbook, answer_position):
    """Trusted task metadata has a separate range count and aggregate cell budget."""
    regions, cells = [], 0
    for address in split_ranges(answer_position, max_ranges=MAX_ANSWER_RANGES):
        worksheet, bounds = resolve(workbook, address)
        left, top, right, bottom = bounds
        cells += (right - left + 1) * (bottom - top + 1)
        if cells > MAX_ANSWER_CELLS:
            raise ToolError(f"At most {MAX_ANSWER_CELLS} total answer cells")
        regions.append((worksheet, bounds))
    return regions


def cell_equal(a, b, expected_epoch=None, produced_epoch=None):
    # Keep the original value contract, including its two-decimal rounding.
    if type(a) is type(b) and type(a) in (str, float, int, bool) and a == b:
        return True
    a, b = normalize(a, expected_epoch), normalize(b, produced_epoch)
    return type(a) is type(b) and a == b


def compare(oracle: Path, actual: Path, answer_position: str):
    expected = load(oracle, values=True, read_only=True)
    produced = None
    matched = total = 0
    try:
        produced = load(actual, values=True, read_only=True)
        for ws, (left, top, right, bottom) in answer_regions(expected, answer_position):
            width = right - left + 1
            total += width * (bottom - top + 1)
            if ws.title not in produced.sheetnames:
                continue
            bounds = dict(
                min_row=top,
                max_row=bottom,
                min_col=left,
                max_col=right,
                values_only=True,
            )
            original_rows = iter(ws.iter_rows(**bounds))
            actual_rows = iter(produced[ws.title].iter_rows(**bounds))
            empty = (None,) * width
            for _ in range(top, bottom + 1):
                originals, actuals = (
                    next(original_rows, empty),
                    next(actual_rows, empty),
                )
                for index in range(width):
                    matched += bool(
                        cell_equal(
                            originals[index],
                            actuals[index],
                            expected.epoch,
                            produced.epoch,
                        )
                    )
        if not total:
            raise ToolError("No answer cells in task metadata")
        return {
            "reward": float(matched == total),
            "matched_cells": matched,
            "total_cells": total,
            "cell_accuracy": matched / total,
        }
    finally:
        expected.close()
        if produced is not None:
            produced.close()
