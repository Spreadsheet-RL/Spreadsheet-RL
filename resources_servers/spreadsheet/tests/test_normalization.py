from datetime import date, datetime, time

import pytest

from resources_servers.spreadsheet.service.workbook import normalize, split_ranges


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42.0),
        ("42.004", 42.0),
        (True, True),
        (None, None),
        ("", None),
        (date(1900, 1, 1), 2.0),
        (datetime(1900, 1, 1, 12), 2.5),
        (time(12), 0.5),
        ("not a number", "not a number"),
        ("nan", "nan"),
    ],
)
def test_normalization_matches_current_reference_contract(value, expected):
    actual = normalize(value)
    assert actual == expected
    assert type(actual) is type(expected)


def test_answer_regions_support_quoted_commas_and_dangling_closing_quote():
    assert split_ranges("'My, Sheet'!A1:B2,Summary'!C3") == ["'My, Sheet'!A1:B2", "Summary'!C3"]
