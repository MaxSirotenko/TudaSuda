"""Streaming persistence primitives for factual registry artifacts.

This module is intentionally unaware of factual contracts and business keys.
`warehouse_factual_data` remains the compatibility façade and owns semantics.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from warehouse_perf_diagnostics import measure, record_artifact_read
from warehouse_persistence import atomic_write_json


def atomic_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value, separators=None)


def write_jsonl(
    path: Path, rows: Iterable[Mapping[str, Any]], *, compresslevel: int = 6
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel) as stream:
        for row in rows:
            stream.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield a gzip JSONL artifact without retaining decoded rows."""
    if not path.exists():
        return
    record_artifact_read(path.stat().st_size)
    with measure("factual.artifact_read"):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Materialized compatibility reader for legacy public callers."""
    return list(iter_jsonl(path))
