from __future__ import annotations

import re
from typing import Any

_FUTURE_FUNCTION_PREFIXES = {
    "FILTER": "_xlfn._xlws.",
    "SORT": "_xlfn.",
    "SORTBY": "_xlfn.",
    "UNIQUE": "_xlfn.",
    "XLOOKUP": "_xlfn.",
    "XMATCH": "_xlfn.",
    "SEQUENCE": "_xlfn.",
    "RANDARRAY": "_xlfn.",
    "TAKE": "_xlfn.",
    "DROP": "_xlfn.",
    "HSTACK": "_xlfn.",
    "VSTACK": "_xlfn.",
    "CHOOSECOLS": "_xlfn.",
    "CHOOSEROWS": "_xlfn.",
    "EXPAND": "_xlfn.",
    "TOCOL": "_xlfn.",
    "TOROW": "_xlfn.",
    "WRAPROWS": "_xlfn.",
    "WRAPCOLS": "_xlfn.",
    "TEXTSPLIT": "_xlfn.",
    "TEXTBEFORE": "_xlfn.",
    "TEXTAFTER": "_xlfn.",
    "BYROW": "_xlfn.",
    "BYCOL": "_xlfn.",
    "LAMBDA": "_xlfn.",
    "LET": "_xlfn.",
    "MAP": "_xlfn.",
    "REDUCE": "_xlfn.",
    "SCAN": "_xlfn.",
    "MAKEARRAY": "_xlfn.",
}

_IDENT_RE = re.compile(r"[A-Za-z_\\][A-Za-z0-9_.]*")
_LAMBDA_PARAM_RE = re.compile(r"(\s*)(?:_xlpm\.)?([A-Za-z_\\][A-Za-z0-9_]*)\s*", re.IGNORECASE)
_DELIMITER_PAIRS = {"(": ")", "{": "}"}
_CLOSING_DELIMITERS = {closing: opening for opening, closing in _DELIMITER_PAIRS.items()}
_SPILL_OPERATOR_RE = re.compile(r"(?<=[A-Za-z0-9_)\]])#")
_FUNCTION_ARITY_BOUNDS = {
    "COUNTIF": (2, 2),
    "COUNTIFS": (2, 254),
    "LOOKUP": (2, 3),
    "NETWORKDAYS": (2, 3),
    "QUOTIENT": (2, 2),
    "SUMIF": (2, 3),
    "SUMIFS": (3, 255),
    "WORKDAY": (2, 3),
}
_FUNCTION_ARGUMENT_PARITY = {
    "COUNTIFS": 0,
    "SUMIFS": 1,
}
_REFERENCE_RETURNING_FUNCTIONS = frozenset({"CHOOSE", "INDEX", "INDIRECT", "OFFSET", "XLOOKUP"})


def _is_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch in {"_", ".", "\\"}


def _copy_quoted(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    j = start + 1
    while j < len(text):
        if text[j] == quote:
            if quote == '"' and j + 1 < len(text) and text[j + 1] == '"':
                j += 2
                continue
            if quote == "'" and j + 1 < len(text) and text[j + 1] == "'":
                j += 2
                continue
            j += 1
            break
        j += 1
    return text[start:j], j


def _copy_bracketed(text: str, start: int) -> tuple[str, int]:
    depth = 0
    j = start
    while j < len(text):
        ch = text[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    return text[start:j], j


def _copy_braced(text: str, start: int) -> tuple[str, int]:
    depth = 0
    j = start
    while j < len(text):
        ch = text[j]
        if ch in {'"', "'"}:
            _, j = _copy_quoted(text, j)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    return text[start:j], j


def _normalize_single_quoted_literals(formula: str) -> str:
    if "'" not in formula:
        return formula

    out: list[str] = []
    i = 0
    total = len(formula)
    while i < total:
        if formula[i] == '"':
            quoted, i = _copy_quoted(formula, i)
            out.append(quoted)
            continue

        if formula[i] == "[":
            bracketed, i = _copy_bracketed(formula, i)
            out.append(bracketed)
            continue
        if formula[i] == "{":
            braced, i = _copy_braced(formula, i)
            if braced.endswith("}") and len(braced) >= 2:
                out.append("{")
                out.append(_normalize_single_quoted_literals(braced[1:-1]))
                out.append("}")
            else:
                out.append(braced)
            continue

        if formula[i] != "'":
            out.append(formula[i])
            i += 1
            continue

        j = i + 1
        content: list[str] = []
        while j < total:
            ch = formula[j]
            if ch == "'":
                if j + 1 < total and formula[j + 1] == "'":
                    content.append("'")
                    j += 2
                    continue
                break
            content.append(ch)
            j += 1

        if j >= total:
            out.append(formula[i])
            i += 1
            continue

        k = j + 1
        while k < total and formula[k].isspace():
            k += 1
        if k < total and formula[k] == "!":
            out.append(formula[i : j + 1])
            i = j + 1
            continue

        literal = "".join(content).replace('"', '""')
        out.append(f'"{literal}"')
        i = j + 1

    return "".join(out)


def _normalize_future_functions(formula: str) -> str:
    out: list[str] = []
    i = 0
    total = len(formula)
    while i < total:
        ch = formula[i]
        if ch in {'"', "'"}:
            quoted, i = _copy_quoted(formula, i)
            out.append(quoted)
            continue
        if ch == "[":
            bracketed, i = _copy_bracketed(formula, i)
            out.append(bracketed)
            continue
        if ch == "{":
            braced, i = _copy_braced(formula, i)
            out.append(braced)
            continue

        match = _IDENT_RE.match(formula, i)
        if match is None:
            out.append(ch)
            i += 1
            continue

        token = match.group(0)
        j = match.end()
        k = j
        while k < total and formula[k].isspace():
            k += 1

        func_name = token.rsplit(".", 1)[-1].upper()
        if k < total and formula[k] == "(" and func_name in _FUTURE_FUNCTION_PREFIXES:
            token_key = token.casefold()
            if (
                "." not in token
                or token_key.startswith("_xludf.")
                or (func_name == "FILTER" and token_key in {"_xlfn.filter", "_xlws.filter"})
            ):
                out.append(f"{_FUTURE_FUNCTION_PREFIXES[func_name]}{func_name}")
            else:
                out.append(token)
        else:
            out.append(token)
        i = j

    return "".join(out)


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(text):
        ch = text[i]
        if ch in {'"', "'"}:
            _, i = _copy_quoted(text, i)
            continue
        if ch == "[":
            _, i = _copy_bracketed(text, i)
            continue
        if ch == "{":
            _, i = _copy_braced(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level_args(text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in {'"', "'"}:
            _, i = _copy_quoted(text, i)
            continue
        if ch == "[":
            _, i = _copy_bracketed(text, i)
            continue
        if ch == "{":
            _, i = _copy_braced(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
        i += 1
    args.append(text[start:])
    return args


def _replace_lambda_names(text: str, names: set[str]) -> str:
    if not names:
        return text

    out: list[str] = []
    i = 0
    total = len(text)
    names_cf = {name.casefold(): name for name in names}
    while i < total:
        ch = text[i]
        if ch in {'"', "'"}:
            quoted, i = _copy_quoted(text, i)
            out.append(quoted)
            continue
        if ch == "[":
            bracketed, i = _copy_bracketed(text, i)
            out.append(bracketed)
            continue
        if ch == "{":
            braced, i = _copy_braced(text, i)
            if braced.endswith("}") and len(braced) >= 2:
                out.append("{")
                out.append(_replace_lambda_names(braced[1:-1], names))
                out.append("}")
            else:
                out.append(braced)
            continue

        match = _IDENT_RE.match(text, i)
        if match is None:
            out.append(ch)
            i += 1
            continue

        token = match.group(0)
        token_key = token.casefold()
        prev = text[i - 1] if i > 0 else ""
        prev_idx = i - 1
        while prev_idx >= 0 and text[prev_idx].isspace():
            prev_idx -= 1
        prev_nonspace = text[prev_idx] if prev_idx >= 0 else ""
        next_idx = match.end()
        next_ch = text[next_idx] if next_idx < total else ""
        while next_idx < total and text[next_idx].isspace():
            next_idx += 1
        next_nonspace = text[next_idx] if next_idx < total else ""
        declared_name = names_cf.get(token_key)
        if declared_name is not None and not (
            prev == "."
            or prev == "$"
            or next_ch == "$"
            or _is_identifier_char(next_ch)
            or prev_nonspace == ":"
            or next_nonspace in {":", "["}
        ):
            out.append(f"_xlpm.{declared_name}")
        else:
            out.append(token)
        i = match.end()

    return "".join(out)


def _normalize_single_lambda_call(inner: str) -> str:
    args = _split_top_level_args(inner)
    if len(args) < 2:
        return inner

    params: list[str] = []
    normalized_args = list(args)
    for idx, arg in enumerate(args[:-1]):
        match = _LAMBDA_PARAM_RE.fullmatch(arg)
        if match is None:
            continue
        name = match.group(2)
        params.append(name)
        normalized_args[idx] = f"{match.group(1)}_xlpm.{name}"

    if params:
        normalized_args[-1] = _replace_lambda_names(args[-1], set(params))
    return ",".join(normalized_args)


def _normalize_single_let_call(inner: str) -> str:
    args = _split_top_level_args(inner)
    if len(args) < 3 or len(args) % 2 == 0:
        return inner

    names: list[str] = []
    normalized_args = list(args)
    for idx in range(0, len(args) - 1, 2):
        if names:
            normalized_args[idx + 1] = _replace_lambda_names(args[idx + 1], set(names))
        match = _LAMBDA_PARAM_RE.fullmatch(args[idx])
        if match is None:
            continue
        name = match.group(2)
        names.append(name)
        normalized_args[idx] = f"{match.group(1)}_xlpm.{name}"

    if names:
        normalized_args[-1] = _replace_lambda_names(args[-1], set(names))
    return ",".join(normalized_args)


def _normalize_scoped_parameters(formula: str) -> str:
    out: list[str] = []
    i = 0
    total = len(formula)
    while i < total:
        ch = formula[i]
        if ch in {'"', "'"}:
            quoted, i = _copy_quoted(formula, i)
            out.append(quoted)
            continue
        if ch == "[":
            bracketed, i = _copy_bracketed(formula, i)
            out.append(bracketed)
            continue
        if ch == "{":
            braced, i = _copy_braced(formula, i)
            out.append(braced)
            continue

        match = _IDENT_RE.match(formula, i)
        if match is None:
            out.append(ch)
            i += 1
            continue

        token = match.group(0)
        j = match.end()
        k = j
        while k < total and formula[k].isspace():
            k += 1

        func_name = token.rsplit(".", 1)[-1].upper()
        if k < total and formula[k] == "(" and func_name in {"LAMBDA", "LET"}:
            close_idx = _find_matching_paren(formula, k)
            if close_idx == -1:
                out.append(token)
                i = j
                continue
            inner = _normalize_scoped_parameters(formula[k + 1 : close_idx])
            out.append(formula[i : k + 1])
            if func_name == "LAMBDA":
                out.append(_normalize_single_lambda_call(inner))
            else:
                out.append(_normalize_single_let_call(inner))
            out.append(")")
            i = close_idx + 1
            continue

        out.append(token)
        i = j

    return "".join(out)


def normalize_formula_for_excel(formula: str) -> str:
    formula = str(formula or "").strip()
    if not formula:
        raise ValueError("formula_template is empty")
    if not formula.startswith("="):
        formula = "=" + formula
    formula = _normalize_single_quoted_literals(formula)
    formula = _normalize_scoped_parameters(formula)
    return _normalize_future_functions(formula)


def _formula_syntax_error(position: int, detail: str) -> ValueError:
    return ValueError(f"formula syntax invalid at position {position}: {detail}")


def _validate_formula_delimiters(formula: str) -> None:
    stack: list[tuple[str, int]] = []
    quote: str | None = None
    quote_start = 0
    i = 0
    while i < len(formula):
        ch = formula[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < len(formula) and formula[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch == "[":
            bracketed, bracket_end = _copy_bracketed(formula, i)
            if not bracketed.endswith("]"):
                raise _formula_syntax_error(i + 1, "unclosed '['")
            i = bracket_end
            continue
        if ch in {'"', "'"}:
            quote = ch
            quote_start = i
        elif ch in _DELIMITER_PAIRS:
            stack.append((ch, i))
        elif ch in _CLOSING_DELIMITERS:
            expected_open = _CLOSING_DELIMITERS[ch]
            if not stack:
                raise _formula_syntax_error(i + 1, f"unexpected {ch!r}")
            opening, opening_position = stack[-1]
            if opening != expected_open:
                expected_close = _DELIMITER_PAIRS[opening]
                raise _formula_syntax_error(
                    i + 1,
                    f"expected {expected_close!r} to close {opening!r} at position {opening_position + 1}",
                )
            stack.pop()
        i += 1

    if quote is not None:
        quote_name = "double quote" if quote == '"' else "single quote"
        raise _formula_syntax_error(quote_start + 1, f"unclosed {quote_name}")
    if stack:
        opening, opening_position = stack[-1]
        raise _formula_syntax_error(opening_position + 1, f"unclosed {opening!r}")


def _token_positions(formula: str, tokens) -> list[int]:
    positions: list[int] = []
    cursor = 1 if formula.startswith("=") else 0
    for token in tokens:
        value = token.value
        position = formula.find(value, cursor)
        if position < 0:
            position = cursor
        positions.append(position)
        cursor = position + len(value)
    return positions


def _function_name_from_token(token_value: str) -> str:
    raw_name = token_value.rstrip("(").rsplit(":", 1)[-1]
    return raw_name.rsplit(".", 1)[-1].upper()


def _matching_open_token_index(positioned: list[tuple[Any, int]], close_index: int) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        token = positioned[index][0]
        if token.subtype == "CLOSE":
            depth += 1
        elif token.subtype == "OPEN":
            depth -= 1
            if depth == 0:
                return index
    return None


def _closed_expression_returns_reference(positioned: list[tuple[Any, int]], close_index: int) -> bool:
    open_index = _matching_open_token_index(positioned, close_index)
    if open_index is None:
        return False

    opening = positioned[open_index][0]
    if opening.type == "FUNC":
        return _function_name_from_token(opening.value) in _REFERENCE_RETURNING_FUNCTIONS
    if opening.type != "PAREN":
        return False

    inner_start = open_index + 1
    inner_end = close_index - 1
    if inner_start == inner_end:
        inner = positioned[inner_start][0]
        return inner.type == "OPERAND" and inner.subtype == "RANGE"
    if inner_start < inner_end and positioned[inner_end][0].subtype == "CLOSE":
        nested_open = _matching_open_token_index(positioned, inner_end)
        if nested_open == inner_start:
            return _closed_expression_returns_reference(positioned, inner_end)
    return False


def _validate_formula_operands(formula: str, tokens) -> None:
    positioned = [
        (token, position)
        for token, position in zip(tokens, _token_positions(formula, tokens), strict=True)
        if token.type != "WHITE-SPACE"
    ]
    if not positioned:
        raise _formula_syntax_error(1, "formula has no expression")

    for index, (token, position) in enumerate(positioned):
        if token.type == "FUNC" and token.subtype == "OPEN" and ":" in token.value[:-1]:
            endpoint_function = _function_name_from_token(token.value)
            if endpoint_function not in _REFERENCE_RETURNING_FUNCTIONS:
                raise _formula_syntax_error(
                    position + token.value.rfind(":") + 2,
                    f"range endpoint function {endpoint_function} is not reference-returning",
                )
        if token.type == "OPERAND" and token.subtype == "RANGE":
            if token.value.strip(".") == "" or token.value.endswith("$"):
                raise _formula_syntax_error(position + 1, f"invalid range/name operand {token.value!r}")
        if token.type == "OPERATOR-INFIX":
            previous = positioned[index - 1][0] if index > 0 else None
            following = positioned[index + 1][0] if index + 1 < len(positioned) else None
            if previous is None or previous.type in {"OPERATOR-INFIX", "OPERATOR-PREFIX", "SEP"}:
                raise _formula_syntax_error(position + 1, f"operator {token.value!r} is missing a left operand")
            if previous.subtype == "OPEN":
                raise _formula_syntax_error(position + 1, f"operator {token.value!r} is missing a left operand")
            if following is None or following.type in {"OPERATOR-INFIX", "OPERATOR-POSTFIX", "SEP"}:
                raise _formula_syntax_error(position + 1, f"operator {token.value!r} is missing a right operand")
            if following.subtype == "CLOSE":
                raise _formula_syntax_error(position + 1, f"operator {token.value!r} is missing a right operand")
        elif token.type == "OPERATOR-PREFIX":
            following = positioned[index + 1][0] if index + 1 < len(positioned) else None
            if following is None or following.type == "SEP" or following.subtype == "CLOSE":
                raise _formula_syntax_error(position + 1, f"operator {token.value!r} is missing an operand")

        if index == 0:
            continue
        previous, previous_position = positioned[index - 1]
        whitespace_between = formula[previous_position + len(previous.value) : position].isspace()
        previous_is_operand = previous.type == "OPERAND"
        previous_is_closed_expression = previous.subtype == "CLOSE"
        previous_is_postfix = previous.type == "OPERATOR-POSTFIX"
        token_starts_expression = token.type == "OPERAND" or token.subtype == "OPEN"
        if not token_starts_expression or not (
            previous_is_operand or previous_is_closed_expression or previous_is_postfix
        ):
            continue
        if previous_is_closed_expression and token.type == "PAREN" and token.subtype == "OPEN":
            continue
        if token.value.startswith(":"):
            range_endpoint = token.value[1:]
            left_returns_reference = (previous.type == "OPERAND" and previous.subtype == "RANGE") or (
                previous_is_closed_expression and _closed_expression_returns_reference(positioned, index - 1)
            )
            right_returns_reference = (token.type == "OPERAND" and token.subtype == "RANGE") or (
                token.type == "FUNC" and _function_name_from_token(token.value) in _REFERENCE_RETURNING_FUNCTIONS
            )
            if (
                range_endpoint
                and not range_endpoint.startswith(":")
                and left_returns_reference
                and right_returns_reference
            ):
                continue
            raise _formula_syntax_error(position + 1, f"missing operator before {token.value!r}")
        if (
            whitespace_between
            and (previous_is_closed_expression or previous.subtype == "RANGE")
            and (token.type == "FUNC" or token.type == "PAREN" or token.subtype == "RANGE")
        ):
            continue
        raise _formula_syntax_error(position + 1, f"missing operator before {token.value!r}")


def _validate_function_arities(formula: str, tokens) -> None:
    frames: list[dict[str, object]] = []
    for token, position in zip(tokens, _token_positions(formula, tokens), strict=True):
        if token.type == "WHITE-SPACE":
            continue
        if token.subtype == "OPEN":
            if frames:
                frames[-1]["has_content"] = True
            frame: dict[str, object] = {"type": token.type, "has_content": False}
            if token.type == "FUNC":
                raw_name = token.value[:-1]
                name_parts = raw_name.split(".")
                compatibility_prefixes = {"_XLFN", "_XLWS"}
                is_builtin = len(name_parts) == 1 or all(
                    part.upper() in compatibility_prefixes for part in name_parts[:-1]
                )
                frame.update(
                    {
                        "name": name_parts[-1].upper() if is_builtin else "",
                        "position": position,
                        "separators": 0,
                    }
                )
            frames.append(frame)
            continue
        if token.subtype == "CLOSE":
            if not frames:
                continue
            frame = frames.pop()
            if frame.get("type") != "FUNC":
                continue
            name = str(frame["name"])
            bounds = _FUNCTION_ARITY_BOUNDS.get(name)
            if bounds is None:
                continue
            separators = int(frame["separators"])
            argument_count = separators + 1 if frame["has_content"] or separators else 0
            minimum, maximum = bounds
            if not minimum <= argument_count <= maximum:
                expected = str(minimum) if minimum == maximum else f"{minimum} to {maximum}"
                raise _formula_syntax_error(
                    int(frame["position"]) + 1,
                    f"function {name} expects {expected} arguments; received {argument_count}",
                )
            parity = _FUNCTION_ARGUMENT_PARITY.get(name)
            if parity is not None and argument_count % 2 != parity:
                pair_kind = "range/criteria pairs" if name == "COUNTIFS" else "criteria range/criteria pairs"
                raise _formula_syntax_error(
                    int(frame["position"]) + 1,
                    f"function {name} expects complete {pair_kind}; received {argument_count} arguments",
                )
            continue
        if token.type == "SEP" and token.subtype == "ARG" and frames and frames[-1].get("type") == "FUNC":
            frames[-1]["separators"] = int(frames[-1]["separators"]) + 1
            continue
        if frames:
            frames[-1]["has_content"] = True


def validate_formula_for_excel(formula: str) -> None:
    """Reject locally detectable formula syntax errors before workbook mutation."""
    _validate_formula_delimiters(formula)
    try:
        from openpyxl.formula import Tokenizer
        from openpyxl.formula.tokenizer import TokenizerError
    except ImportError:
        raise RuntimeError("openpyxl is required to validate formulas; install openpyxl>=3.1.5") from None

    try:
        tokenizer = Tokenizer(_SPILL_OPERATOR_RE.sub("%", formula))
    except (IndexError, TokenizerError, ValueError) as exc:
        raise _formula_syntax_error(len(formula), str(exc)) from None
    _validate_formula_operands(formula, tokenizer.items)
    _validate_function_arities(formula, tokenizer.items)


def fill_formula(sheet, start_cell: str, end_row: int, end_col: str, formula_template: str) -> tuple[int, int]:
    """Fill a formula over an inclusive rectangular range.

    Returns (filled_cells, skipped_merged_cells).
    """
    try:
        from openpyxl.cell.cell import MergedCell
        from openpyxl.formula.translate import Translator
        from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
    except ImportError:
        raise RuntimeError("openpyxl is required to fill formulas; install openpyxl>=3.1.5") from None

    normalized_start = start_cell.strip().upper().replace("$", "")
    start_col_letter, start_row = coordinate_from_string(normalized_start)
    start_col_idx = column_index_from_string(start_col_letter)
    if end_row < start_row:
        raise ValueError(f"end_row ({end_row}) must be >= start_cell row ({start_row})")

    end_col_norm = str(end_col or "").strip().upper().replace("$", "")
    if not end_col_norm:
        raise ValueError("end_col is empty")
    end_col_idx = column_index_from_string(end_col_norm)
    if end_col_idx < start_col_idx:
        raise ValueError(f"end_col ({end_col_norm}) must be >= start_cell col ({start_col_letter})")

    formula = normalize_formula_for_excel(formula_template)
    validate_formula_for_excel(formula)

    if end_row == start_row and end_col_idx == start_col_idx:
        cell = sheet.cell(row=start_row, column=start_col_idx)
        if isinstance(cell, MergedCell):
            raise ValueError(f"start_cell {normalized_start!r} is a merged cell; use the top-left cell")
        cell.value = formula
        return 1, 0

    translator = Translator(formula, origin=normalized_start)

    filled_cells = 0
    skipped_merged_cells = 0
    for row in range(start_row, end_row + 1):
        row_delta = row - start_row
        for col_idx in range(start_col_idx, end_col_idx + 1):
            col_delta = col_idx - start_col_idx
            cell = sheet.cell(row=row, column=col_idx)
            if isinstance(cell, MergedCell):
                if row_delta == 0 and col_delta == 0:
                    raise ValueError(f"start_cell {normalized_start!r} is a merged cell; use the top-left cell")
                skipped_merged_cells += 1
                continue

            if row_delta == 0 and col_delta == 0:
                cell.value = formula
            else:
                cell.value = translator.translate_formula(
                    row_delta=row_delta,
                    col_delta=col_delta,
                )
            filled_cells += 1

    return filled_cells, skipped_merged_cells
