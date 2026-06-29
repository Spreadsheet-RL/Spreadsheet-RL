from __future__ import annotations

import re


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


def fill_formula(sheet, start_cell: str, end_row: int, end_col: str, formula_template: str) -> tuple[int, int]:
    """Fill a formula over an inclusive rectangular range.

    Returns (filled_cells, skipped_merged_cells).
    """
    try:
        from openpyxl.formula.translate import Translator
        from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
        from openpyxl.cell.cell import MergedCell
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
