"""Streamlit-free orchestration for revision-aware geometry layer rendering."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from warehouse_performance import measure_step
from warehouse_revisions import revision_token_from_state


def render_geometry_layers(
    model: dict[str, Any], placement_state: dict[str, Any], settings: dict[str, Any],
    *, model_id: str, revision_state_loader: Callable[[str], dict[str, Any]],
    static_revision_domains: tuple[str, ...], dynamic_revision_domains: tuple[str, ...],
    static_builder: Callable[..., str], static_cached_builder: Callable[..., str],
    dynamic_builder: Callable[..., dict[str, Any]], dynamic_cached_builder: Callable[..., dict[str, Any]],
    composer: Callable[[str, dict[str, Any]], str], serializer: Callable[[Any], str],
    scale: float, detailed: bool, static_version: int, dynamic_version: int,
) -> dict[str, Any]:
    with measure_step("geometry_render_token"):
        revision_state = revision_state_loader(model_id)
        static_token = None if revision_state.get("warning") else revision_token_from_state(
            revision_state, static_revision_domains
        )
        dynamic_token = None if revision_state.get("warning") else revision_token_from_state(
            revision_state, dynamic_revision_domains
        )
    with measure_step("build_geometry_static_layer"):
        static = (static_builder(model, scale, detailed, settings) if static_token is None else
                  static_cached_builder(model, static_token, settings, scale, detailed, static_version))
    with measure_step("build_geometry_dynamic_layer"):
        dynamic = (dynamic_builder(model, placement_state, settings) if dynamic_token is None else
                   dynamic_cached_builder(model, placement_state, dynamic_token, settings, dynamic_version))
    dynamic_bytes = len(serializer(dynamic).encode("utf-8"))
    with measure_step("compose_geometry_layers"):
        html = composer(static, dynamic)
    return {
        "html": html, "warning": str(revision_state.get("warning") or ""),
        "static_token": static_token, "dynamic_token": dynamic_token,
        "static_size_bytes": len(static.encode("utf-8")), "dynamic_size_bytes": dynamic_bytes,
        "final_size_bytes": len(html.encode("utf-8")), "dynamic_cells_count": len(dynamic),
    }
