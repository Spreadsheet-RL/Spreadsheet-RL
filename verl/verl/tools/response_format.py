from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def records_to_csv(records: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for record in records:
        writer.writerow([_csv_cell(record.get(column)) for column in columns])
    text = out.getvalue()
    if text.endswith("\n"):
        return text[:-1]
    return text
