"""Persistent, lightweight revision counters for warehouse data domains.

Reading is deliberately side-effect free.  The state file is created only by an
explicit initialization or by a successful revision bump.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA_VERSION = 1
REVISION_PATH = Path("data/last_import/data_revisions.json")
REVISION_DOMAINS = (
    "geometry",
    "placements",
    "receipts",
    "inventory",
    "outbound",
    "render_settings",
)

_LOCK = threading.RLock()


def resolve_model_id(model: Mapping | object | None = None) -> str:
    """Return the active model's stable id with the documented fallbacks."""
    if isinstance(model, Mapping):
        value = model.get("model_id") or model.get("source_file_hash")
    else:
        value = getattr(model, "model_id", None) or getattr(model, "source_file_hash", None)
        if value is None and isinstance(model, str):
            value = model
    return str(value or "active")


def default_revision_state(model_id: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": resolve_model_id(model_id),
        "updated_at": "",
        "revisions": {domain: 0 for domain in REVISION_DOMAINS},
        "last_change": {"domains": [], "reason": "", "changed_at": ""},
        "warning": "",
    }


def _domains(domains: Iterable[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(domains))
    unknown = [domain for domain in result if domain not in REVISION_DOMAINS]
    if unknown:
        raise ValueError(f"Unknown revision domain(s): {', '.join(map(str, unknown))}")
    return result


def _counter(value: object) -> int:
    # bool is intentionally rejected even though it subclasses int.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _normalize(payload: object, model_id: str | None) -> dict:
    requested_id = resolve_model_id(model_id)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        state = default_revision_state(requested_id)
        state["warning"] = "Файл ревизий повреждён или имеет неподдерживаемую структуру."
        return state
    stored_id = resolve_model_id(payload.get("model_id"))
    if stored_id != requested_id:
        return default_revision_state(requested_id)
    state = default_revision_state(requested_id)
    revisions = payload.get("revisions") if isinstance(payload.get("revisions"), dict) else {}
    state["revisions"] = {domain: _counter(revisions.get(domain)) for domain in REVISION_DOMAINS}
    state["updated_at"] = str(payload.get("updated_at") or "")
    change = payload.get("last_change") if isinstance(payload.get("last_change"), dict) else {}
    raw_domains = change.get("domains") if isinstance(change.get("domains"), list) else []
    state["last_change"] = {
        "domains": [domain for domain in raw_domains if domain in REVISION_DOMAINS],
        "reason": str(change.get("reason") or ""),
        "changed_at": str(change.get("changed_at") or ""),
    }
    return state


def _load_unlocked(model_id: str | None) -> dict:
    if not REVISION_PATH.exists():
        return default_revision_state(model_id)
    try:
        payload = json.loads(REVISION_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        state = default_revision_state(model_id)
        state["warning"] = f"Не удалось прочитать файл ревизий: {exc}"
        return state
    return _normalize(payload, model_id)


def load_revision_state(model_id: str | None = None) -> dict:
    with _LOCK:
        return _load_unlocked(model_id)


def get_revision(model_id: str | None, domain: str) -> int:
    checked = _domains([domain])[0]
    return load_revision_state(model_id)["revisions"][checked]


def get_revision_token(model_id: str | None, domains: Iterable[str]) -> tuple:
    checked = _domains(domains)
    state = load_revision_state(model_id)
    return (state["model_id"], *(state["revisions"][domain] for domain in checked))


def _write_unlocked(state: dict) -> None:
    REVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REVISION_PATH.with_name(
        f".{REVISION_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    persisted = {key: value for key, value in state.items() if key != "warning"}
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(persisted, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, REVISION_PATH)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def initialize_revision_state(model_id: str | None) -> dict:
    """Persist a clean state for the active model (an explicit write action)."""
    with _LOCK:
        state = _load_unlocked(model_id)
        if REVISION_PATH.exists() and not state.get("warning"):
            try:
                stored = json.loads(REVISION_PATH.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                stored = {}
            if resolve_model_id(stored.get("model_id")) == resolve_model_id(model_id):
                return state
        state["warning"] = ""
        _write_unlocked(state)
        return state


def bump_revisions(model_id: str | None, domains: Iterable[str], reason: str = "") -> dict:
    checked = _domains(domains)
    if not checked:
        raise ValueError("At least one revision domain is required")
    with _LOCK:
        state = _load_unlocked(model_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for domain in checked:
            state["revisions"][domain] += 1
        state["updated_at"] = now
        state["last_change"] = {
            "domains": list(checked),
            "reason": str(reason or "")[:200],
            "changed_at": now,
        }
        state["warning"] = ""
        _write_unlocked(state)
        return state
