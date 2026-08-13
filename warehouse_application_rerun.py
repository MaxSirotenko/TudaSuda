"""Testable orchestration boundary for a warm, non-mutating map rerun."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def execute_warm_map_rerun(
    *,
    model: dict[str, Any],
    placement_state: dict[str, Any] | None,
    settings: dict[str, Any],
    static_token: tuple,
    dynamic_token: tuple,
    static_loader: Callable[..., str],
    dynamic_loader: Callable[..., dict[str, Any]],
    composer: Callable[[str, dict[str, Any]], str],
    static_version: int,
    dynamic_version: int,
    state_loader: Callable[[dict[str, Any]], tuple[dict[str, Any], str | None]] | None = None,
    scale: float = 18.0,
    detailed: bool = True,
) -> dict[str, Any]:
    """Execute the production cache/compose sequence without UI side effects.

    Heavy operations such as factual readiness, optimizer and physical graph
    construction are intentionally absent: a normal map render has no reason to
    invoke them.  The returned values are aggregate diagnostics only.
    """
    state_reads = 0
    if state_loader is not None:
        placement_state, warning = state_loader(model)
        state_reads = 1
        if warning:
            placement_state = {}
    placement_state = placement_state or {}
    static = static_loader(
        model, static_token, settings, scale, detailed, static_version
    )
    dynamic = dynamic_loader(
        model, placement_state, dynamic_token, settings, dynamic_version
    )
    html = composer(static, dynamic)
    return {
        "generated_payload_bytes": len(html.encode("utf-8")),
        "dynamic_cells": len(dynamic),
        "state_loader_calls": state_reads,
    }
