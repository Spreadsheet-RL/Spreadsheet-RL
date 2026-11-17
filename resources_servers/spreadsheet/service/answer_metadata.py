"""Answer metadata parsing ported from reward/async_reward_api/eval.py.

Keep the upstream sheet/quote/shorthand/column-only semantics separate from the
public finite-range tool resolver. Equality and rounding are not defined here.
"""

import re

from .errors import ToolError

AnswerPositionError = ToolError
_EXCEL_MAX_COLUMN = 16384
_EXCEL_MAX_ROW = 1048576
_A1_CELL_RE = r"[A-Z]{1,3}[0-9]{1,7}"
_A1_RANGE_RE = re.compile(rf"^{_A1_CELL_RE}(?::{_A1_CELL_RE})?$")
_A1_CELL_COORD_RE = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")
_A1_COLUMN_RANGE_RE = re.compile(r"^[A-Z]{1,3}:[A-Z]{1,3}$")
_A1_RANGE_START_RE = re.compile(rf"^{_A1_CELL_RE}")


def col_name2num(name):
    number = 0
    for char in name:
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _resolve_sheet_name(workbook, sheet_name: str) -> str | None:
    if sheet_name in workbook.sheetnames:
        return sheet_name
    folded = sheet_name.casefold()
    for candidate in workbook.sheetnames:
        if candidate.casefold() == folded:
            return candidate
    stripped = sheet_name.strip()
    if stripped and stripped != sheet_name:
        if stripped in workbook.sheetnames:
            return stripped
        stripped_folded = stripped.casefold()
        for candidate in workbook.sheetnames:
            if candidate.casefold() == stripped_folded:
                return candidate
    return None


def _split_answer_position(answer_position: str) -> list[str]:
    """
    Split an answer_position string into individual sheet/range tokens.

    We split on commas, but allow commas inside a quoted sheet name, e.g.:
      'My, Sheet'!A1:B2

    We intentionally only treat a leading quote (after optional whitespace) as the
    start of a quoted sheet name. This keeps us resilient to malformed inputs
    like: Sheet1'!A1:B2 (dangling apostrophe).
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_quoted_sheet_name = False
    at_token_start = True

    def _flush_token() -> None:
        token = "".join(buf).strip()
        if token:
            tokens.append(token)

    i = 0
    while i < len(answer_position):
        ch = answer_position[i]

        # Allow benign wrappers (whitespace, double-quotes) before the token proper.
        # This helps with inputs like:  "'My, Sheet'!A1:B2,'Other'!C3:D4"
        if at_token_start and (ch.isspace() or ch == '"'):
            buf.append(ch)
            i += 1
            continue

        if at_token_start and ch == "'":
            in_quoted_sheet_name = True
            at_token_start = False
            buf.append(ch)
            i += 1
            continue

        at_token_start = False

        if in_quoted_sheet_name and ch == "'":
            # Excel escapes an apostrophe inside a quoted sheet name as ''.
            if i + 1 < len(answer_position) and answer_position[i + 1] == "'":
                buf.append("'")
                buf.append("'")
                i += 2
                continue

            # End quoted sheet name if this quote is followed by:
            #   - a sheet separator (!), or
            #   - a token separator (,), or
            #   - a direct range start (malformed form like: 'Sheet!'A1:B2), or
            #   - the end of the string
            lookahead = answer_position[i + 1 :].lstrip()
            if (
                not lookahead
                or lookahead.startswith(("!", ","))
                or _A1_RANGE_START_RE.match(lookahead) is not None
            ):
                in_quoted_sheet_name = False

            buf.append(ch)
            i += 1
            continue

        if ch == "," and not in_quoted_sheet_name:
            _flush_token()
            buf = []
            at_token_start = True
            i += 1
            continue

        buf.append(ch)
        i += 1

    _flush_token()
    return tokens


def _normalize_sheet_name(sheet_name: str) -> str:
    # Drop obvious wrapper quotes first (common when passing through JSON/CLI).
    name = sheet_name.strip(' \t\n\r\f\v"')

    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        # Unwrap and unescape Excel-style '' -> '
        name = name[1:-1].replace("''", "'")
        return name

    # Handle malformed/dangling apostrophes like: Sheet1'!A1
    if name.startswith("'") and not name.endswith("'"):
        name = name[1:]
    elif name.endswith("'") and not name.startswith("'"):
        name = name[:-1]
    return name.strip()


def _normalize_cell_range(cell_range: str) -> str:
    # Drop obvious wrapper quotes first (common when passing through JSON/CLI).
    rng = cell_range.strip(" \t\n\r\f\v\"'")
    rng = rng.replace(" ", "")
    rng = rng.replace("\uff1a", ":")
    rng = rng.replace("：", ":")
    rng = rng.upper().replace("$", "")

    # Recover malformed shorthand like "BD2:308" -> "BD2:BD308".
    match = re.fullmatch(r"^([A-Z]{1,3})([0-9]{1,7}):([0-9]{1,7})$", rng)
    if match is not None:
        col = match.group(1)
        start_row = match.group(2)
        end_row = match.group(3)
        return f"{col}{start_row}:{col}{end_row}"

    return rng


def _normalize_answer_position_text(answer_position: str) -> str:
    text = answer_position
    # Normalize common Unicode punctuation variants seen in dataset sources.
    text = text.replace("\uff1a", ":")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("：", ":")
    text = text.replace("‘", "'").replace("’", "'")
    return text


def _is_supported_cell_range(cell_range: str) -> bool:
    if not (
        _A1_RANGE_RE.fullmatch(cell_range) or _A1_COLUMN_RANGE_RE.fullmatch(cell_range)
    ):
        return False
    try:
        _cell_range_boundaries(
            cell_range,
            max_row_hint=1 if _A1_COLUMN_RANGE_RE.fullmatch(cell_range) else None,
        )
    except AnswerPositionError:
        return False
    return True


def parse_answer_position(
    answer_position: str, *, default_sheet_name: str
) -> list[tuple[str, str]]:
    """
    Parse answer_position into a list of (sheet_name, cell_range) pairs.

    Supported forms:
      - A1
      - A1:B2
      - Sheet1!A1
      - Sheet1!A1:B2
      - 'Sheet With Spaces'!A1:B2

    Multiple tokens can be separated by commas.
    """
    if not isinstance(answer_position, str) or not answer_position.strip():
        raise AnswerPositionError("answer_position is empty")

    answer_position = _normalize_answer_position_text(answer_position)
    tokens = _split_answer_position(answer_position)

    parsed: list[tuple[str, str]] = []
    for token in tokens:
        token = token.strip(' \t\n\r\f\v"')
        if not token:
            continue

        if "!" in token:
            if token.count("!") > 1:
                sheet_part, range_part = token.split("!", 1)
                # Recover malformed pattern like "'Received'!'Received!A1:G16'".
                # We only salvage when extra "!" appears inside a quoted range fragment.
                if "!" in range_part and range_part.lstrip().startswith("'"):
                    range_part = range_part.rsplit("!", 1)[-1]
                else:
                    raise AnswerPositionError(
                        f"Malformed sheet reference (multiple '!' characters): {token!r}"
                    )
            else:
                sheet_part, range_part = token.split("!", 1)
            sheet_name = _normalize_sheet_name(sheet_part)
            if not sheet_name:
                raise AnswerPositionError(f"Empty sheet name in token: {token!r}")
        else:
            sheet_name = default_sheet_name
            range_part = token

        cell_range = _normalize_cell_range(range_part)
        if not _is_supported_cell_range(cell_range):
            raise AnswerPositionError(
                f"Invalid cell range: {range_part!r} (normalized: {cell_range!r})"
            )

        parsed.append((sheet_name, cell_range))

    if not parsed:
        raise AnswerPositionError("answer_position is empty")

    return parsed


def _cell_range_boundaries(
    cell_range: str,
    *,
    max_row_hint: int | None = None,
) -> tuple[int, int, int, int]:
    if _A1_COLUMN_RANGE_RE.fullmatch(cell_range):
        start_col_name, end_col_name = cell_range.split(":", 1)
        start_col = col_name2num(start_col_name)
        end_col = col_name2num(end_col_name)
        min_col, max_col = sorted((start_col, end_col))
        if min_col < 1 or max_col > _EXCEL_MAX_COLUMN:
            raise AnswerPositionError(f"Column out of Excel bounds: {cell_range!r}")

        if max_row_hint is None:
            raise AnswerPositionError(
                f"Column-only range requires max_row_hint: {cell_range!r}"
            )
        max_row = max(1, int(max_row_hint))
        return min_col, 1, max_col, max_row

    if ":" in cell_range:
        start_cell, end_cell = cell_range.split(":", 1)
    else:
        start_cell, end_cell = cell_range, cell_range

    match_start = _A1_CELL_COORD_RE.fullmatch(start_cell)
    match_end = _A1_CELL_COORD_RE.fullmatch(end_cell)
    if match_start is None or match_end is None:
        raise AnswerPositionError(f"Invalid cell range: {cell_range!r}")

    start_col = col_name2num(match_start.group(1))
    start_row = int(match_start.group(2))
    end_col = col_name2num(match_end.group(1))
    end_row = int(match_end.group(2))

    min_col, max_col = sorted((start_col, end_col))
    min_row, max_row = sorted((start_row, end_row))
    if min_col < 1 or max_col > _EXCEL_MAX_COLUMN:
        raise AnswerPositionError(f"Column out of Excel bounds: {cell_range!r}")
    if min_row < 1 or max_row > _EXCEL_MAX_ROW:
        raise AnswerPositionError(f"Row out of Excel bounds: {cell_range!r}")
    return min_col, min_row, max_col, max_row
