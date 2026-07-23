from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import _get_sample_meta_cache_size
from .eval import AnswerPositionError, parse_answer_position
from .inflight_cache import Inflight
from .inflight_cache import InflightLruCache


_SampleMetadataInflight = Inflight  # compat alias for tests


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_attachment_names(path: Path) -> list[str]:
    attachments: list[str] = []
    for entry in path.iterdir():
        if entry.is_file() and entry.suffix.lower() in {".xlsx", ".csv"}:
            attachments.append(entry.name)
    return attachments

@dataclass(frozen=True)
class _RewardSampleMetadata:
    answer_position: str
    primary_ext: str
    gt_file: Path


_SAMPLE_META_INFLIGHT_WAIT_S = 30.0
_SAMPLE_META_LRU_CACHE: InflightLruCache[tuple[str, int, int], _RewardSampleMetadata] = InflightLruCache(
    max_size_getter=_get_sample_meta_cache_size,
    inflight_wait_s_getter=lambda: _SAMPLE_META_INFLIGHT_WAIT_S,
)
_SAMPLE_META_CACHE_LOCK = _SAMPLE_META_LRU_CACHE.lock
_SAMPLE_META_CACHE = _SAMPLE_META_LRU_CACHE.cache
_SAMPLE_META_INFLIGHT = _SAMPLE_META_LRU_CACHE.inflight


def _resolve_sample_metadata(sample_dir: Path) -> _RewardSampleMetadata:
    instruction_path = sample_dir / "instruction.json"
    try:
        instruction = _read_json(instruction_path)
        answer_position = str(instruction["answer_position"])
        parse_answer_position(answer_position, default_sheet_name="Sheet1")
    except OSError as exc:
        raise RuntimeError(f"Failed to read instruction.json: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid instruction.json: {exc}") from exc
    except AnswerPositionError as exc:
        raise ValueError(f"Invalid answer_position: {exc}") from exc

    try:
        attachments = _list_attachment_names(sample_dir)
    except OSError as exc:
        raise RuntimeError(f"Failed to list attachments in {sample_dir}: {exc}") from exc
    if not attachments:
        raise ValueError(f"No .xlsx or .csv attachments found in {sample_dir}")

    target_attachments = [
        name
        for name in attachments
        if Path(name).name.lower() in {"target.xlsx", "target.csv"}
    ]
    if not target_attachments:
        raise ValueError("No target.xlsx or target.csv attachment found")
    ranked = sorted(target_attachments, key=lambda n: (0 if n.lower().endswith(".xlsx") else 1, n.lower()))
    primary_name = ranked[0]
    primary_ext = Path(primary_name).suffix.lower().lstrip(".")
    return _RewardSampleMetadata(
        answer_position=answer_position,
        primary_ext=primary_ext,
        gt_file=sample_dir / primary_name,
    )


def _get_cached_sample_metadata(sample_dir: Path) -> _RewardSampleMetadata:
    try:
        sample_stat = sample_dir.stat()
        instruction_stat = (sample_dir / "instruction.json").stat()
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_stat.st_mtime_ns),
            int(instruction_stat.st_mtime_ns),
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to read sample metadata from {sample_dir}: {exc}") from exc

    def _cached_metadata_target_exists(metadata: _RewardSampleMetadata) -> bool:
        try:
            return metadata.gt_file.is_file()
        except OSError:
            return False

    return _SAMPLE_META_LRU_CACHE.get_or_compute(
        cache_key,
        lambda: _resolve_sample_metadata(sample_dir),
        validate_hit=_cached_metadata_target_exists,
    )
