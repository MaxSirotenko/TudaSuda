"""Small, dependency-free persistence primitives for warehouse artifacts.

The helpers intentionally do not know artifact schemas.  Domain modules remain
responsible for validation while this module guarantees durable, replace-based
publication and predictable recovery from missing or malformed JSON.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from warehouse_perf_diagnostics import record_file_read


def read_json(path: Path, *, default: Any = None, encoding: str = "utf-8-sig") -> Any:
    """Read JSON, returning *default* only for a missing file.

    Parse, permission and encoding failures remain visible to callers; silently
    treating corruption as an empty artifact would hide operational data loss.
    """
    try:
        with path.open("r", encoding=encoding) as stream:
            value = json.load(stream)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            record_file_read("json", size)
            return value
    except FileNotFoundError:
        return default


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    ensure_ascii: bool = False,
    separators: tuple[str, str] | None = (",", ":"),
) -> None:
    """Durably publish JSON with an adjacent temporary and ``os.replace``.

    The temporary is closed before replacement, which is required on Windows.
    A failed serialization/publication leaves the previous artifact untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    destination_mode: int | None = None
    try:
        destination_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, ensure_ascii=ensure_ascii, separators=separators)
            stream.flush()
            os.fsync(stream.fileno())
        if destination_mode is not None:
            try:
                os.chmod(temporary, destination_mode)
            except (NotImplementedError, OSError):
                # Permission preservation is best-effort on platforms/filesystems
                # without POSIX-compatible chmod semantics (notably Windows).
                if os.name == "posix":
                    raise
        os.replace(temporary, path)
        if os.name == "posix":
            # File fsync does not make the directory entry durable.  Filesystems
            # that cannot open/fsync directories are allowed to decline safely.
            directory_fd: int | None = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                os.fsync(directory_fd)
            except (NotImplementedError, OSError):
                pass
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
    except BaseException:
        # fdopen owns the descriptor once entered.  If it failed before that,
        # closing an already-closed descriptor is harmlessly ignored.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def file_signature(path: Path) -> tuple[bool, int, int]:
    """Return an inexpensive cache key without reading file contents."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, 0, 0
    return True, stat.st_mtime_ns, stat.st_size


def iter_json_lines(path: Path) -> Iterator[Any]:
    """Lazily iterate UTF-8 JSONL; ``.gz`` artifacts are opened transparently."""
    opener = gzip.open if path.suffix.lower() == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8-sig") as stream:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        record_file_read("jsonl", size)
        for line_number, line in enumerate(stream, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
