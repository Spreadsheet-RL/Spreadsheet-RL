import pytest

from resources_servers.spreadsheet.packaging.audit_selection import analyze, range_bin


def test_selection_report_retains_withheld_tasks_and_separates_strict_recovery():
    tasks = [
        {"task_id": "source/forum/a", "split": "train", "answer_position": "Sheet!A1"},
        {"task_id": "source/forum/b", "split": "test", "answer_position": "Sheet!A1"},
    ]
    rejected = [{"task_id": "source/forum/b", "verdict": "recovered_strict"}]
    summary = {"checked": 2, "baseline_rejected": 1, "identity": {"source_commit": "pinned"}}
    result = analyze(tasks, rejected, summary)
    bucket = result["groups"]["source"]["source/forum"]
    assert bucket["baseline_keep_rate"] == 0.5
    assert bucket["keep_rate_after_strict_recovery"] == 1
    assert result["groups"]["split"]["test"]["strict_recovered"] == 1
    rejected[0]["verdict"] = "numeric_precision_candidate"
    assert analyze(tasks, rejected, summary)["groups"]["split"]["test"]["still_withheld"] == 1
    with pytest.raises(ValueError, match="Duplicate or unknown"):
        analyze(tasks, [*rejected, *rejected], summary)


def test_range_count_bins_measure_metadata_complexity_without_changing_parser():
    assert range_bin("'Sales, East'!A1,'O''Brien'!A1") == "2-5"
    assert range_bin(",".join(["Sheet!A1"] * 24)) == "21+"
    assert range_bin("'unclosed") == "invalid_metadata"
