"""Revision-aware read-through caches for persisted warehouse UI state."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

import streamlit as st

import warehouse_inventory_placement as placement
import warehouse_outbound_orders as outbound
import warehouse_receipts as receipts
import warehouse_revisions as revisions


CACHE_MAX_ENTRIES = 16
MISSING_FILE_SIGNATURE = (False, 0, 0)

T = TypeVar("T")


def get_file_signature(path: Path) -> tuple[bool, int, int]:
    """Return a small content-change proxy without opening *path*."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return MISSING_FILE_SIGNATURE
    return (True, stat.st_mtime_ns, stat.st_size)


def _model_identity(model: dict[str, Any] | object | None) -> tuple[str, str]:
    if isinstance(model, dict):
        model_id = model.get("model_id")
        source_file_hash = model.get("source_file_hash", "")
    else:
        model_id = getattr(model, "model_id", None)
        source_file_hash = getattr(model, "source_file_hash", "")
    return str(model_id or source_file_hash or "active"), str(source_file_hash or "")


def _minimal_model(model_id: str, source_file_hash: str) -> dict[str, str]:
    return {"model_id": model_id, "source_file_hash": source_file_hash}


def _cache_inputs(
    model_id: str, domains: tuple[str, ...], path: Path
) -> tuple[tuple, tuple[bool, int, int]] | None:
    revision_state = revisions.load_revision_state(model_id)
    if revision_state.get("warning"):
        return None
    token = (
        revision_state["model_id"],
        *(revision_state["revisions"][domain] for domain in domains),
    )
    try:
        signature = get_file_signature(path)
    except OSError:
        return None
    return token, signature


@st.cache_data(show_spinner=False, max_entries=CACHE_MAX_ENTRIES)
def _load_placement_state_cached(
    model_id: str,
    source_file_hash: str,
    revision_token: tuple,
    file_signature: tuple[bool, int, int],
):
    return placement.load_placement_state(_minimal_model(model_id, source_file_hash))


@st.cache_data(show_spinner=False, max_entries=CACHE_MAX_ENTRIES)
def _load_receipts_state_cached(
    model_id: str,
    source_file_hash: str,
    revision_token: tuple,
    file_signature: tuple[bool, int, int],
):
    return receipts.load_receipts_state(_minimal_model(model_id, source_file_hash))


@st.cache_data(show_spinner=False, max_entries=CACHE_MAX_ENTRIES)
def _load_outbound_orders_cached(
    model_id: str,
    source_file_hash: str,
    revision_token: tuple,
    file_signature: tuple[bool, int, int],
):
    return outbound.load_outbound_orders(_minimal_model(model_id, source_file_hash))


@st.cache_data(show_spinner=False, max_entries=CACHE_MAX_ENTRIES)
def _load_outbound_execution_state_cached(
    model_id: str,
    source_file_hash: str,
    revision_token: tuple,
    file_signature: tuple[bool, int, int],
):
    return outbound.load_outbound_execution_state(_minimal_model(model_id, source_file_hash))


@st.cache_data(show_spinner=False, max_entries=CACHE_MAX_ENTRIES)
def _load_outbound_execution_log_cached(
    model_id: str,
    source_file_hash: str,
    revision_token: tuple,
    file_signature: tuple[bool, int, int],
):
    return outbound.load_outbound_execution_log(_minimal_model(model_id, source_file_hash))


def _load_cached(
    model: dict[str, Any] | object | None,
    domains: tuple[str, ...],
    path: Path,
    cached_loader: Callable[[str, str, tuple, tuple[bool, int, int]], T],
    direct_loader: Callable[[dict[str, str]], T],
) -> T:
    model_id, source_file_hash = _model_identity(model)
    inputs = _cache_inputs(model_id, domains, path)
    if inputs is None:
        return direct_loader(_minimal_model(model_id, source_file_hash))
    revision_token, file_signature = inputs
    return cached_loader(model_id, source_file_hash, revision_token, file_signature)


def load_placement_state_cached(model):
    return _load_cached(
        model,
        ("placements", "inventory"),
        placement.PLACEMENTS_PATH,
        _load_placement_state_cached,
        placement.load_placement_state,
    )


def load_receipts_state_cached(model):
    return _load_cached(
        model,
        ("receipts",),
        receipts.RECEIPTS_PATH,
        _load_receipts_state_cached,
        receipts.load_receipts_state,
    )


def load_outbound_orders_cached(model):
    return _load_cached(
        model,
        ("outbound",),
        outbound.OUTBOUND_ORDERS_PATH,
        _load_outbound_orders_cached,
        outbound.load_outbound_orders,
    )


def load_outbound_execution_state_cached(model):
    return _load_cached(
        model,
        ("outbound",),
        outbound.OUTBOUND_EXECUTION_STATE_PATH,
        _load_outbound_execution_state_cached,
        outbound.load_outbound_execution_state,
    )


def load_outbound_execution_log_cached(model):
    return _load_cached(
        model,
        ("outbound",),
        outbound.OUTBOUND_EXECUTION_LOG_PATH,
        _load_outbound_execution_log_cached,
        outbound.load_outbound_execution_log,
    )
