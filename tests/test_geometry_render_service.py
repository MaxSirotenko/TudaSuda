from __future__ import annotations

import warehouse_revisions as revisions
from warehouse_geometry_render_service import render_geometry_layers
from warehouse_perf_diagnostics import capture_io_reads


def test_warm_production_render_reads_revision_once_and_does_not_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(revisions, "REVISION_PATH", tmp_path / "revisions.json")
    revisions.initialize_revision_state("model-a")
    rebuilds = {"static": 0, "dynamic": 0}

    def static_rebuild(*args, **kwargs):
        rebuilds["static"] += 1
        return "unexpected"

    def dynamic_rebuild(*args, **kwargs):
        rebuilds["dynamic"] += 1
        return {}

    with capture_io_reads() as reads:
        rendered = render_geometry_layers(
            {"model_id": "model-a"}, {}, {}, model_id="model-a",
            revision_state_loader=revisions.load_revision_state,
            static_revision_domains=("geometry", "render_settings"),
            dynamic_revision_domains=("geometry", "placements", "outbound", "render_settings"),
            static_builder=static_rebuild,
            static_cached_builder=lambda *args: "__WAREHOUSE_DYNAMIC_STATE__",
            dynamic_builder=dynamic_rebuild,
            dynamic_cached_builder=lambda *args: {},
            composer=lambda static, dynamic: static.replace("__WAREHOUSE_DYNAMIC_STATE__", "{}"),
            serializer=lambda value: "{}", scale=18.0, detailed=True,
            static_version=1, dynamic_version=2,
        )

    assert reads["reader:json"] == 1
    assert reads["file_reads"] == 1
    assert rebuilds == {"static": 0, "dynamic": 0}
    assert rendered["static_token"] == ("model-a", 0, 0)
    assert rendered["dynamic_token"] == ("model-a", 0, 0, 0, 0)
