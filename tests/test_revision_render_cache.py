import ast
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import virtual_warehouse_app as app
import warehouse_revisions as revisions


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(revisions, "REVISION_PATH", tmp_path / "data_revisions.json")
    app.build_geometry_html_cached.clear()
    yield
    app.build_geometry_html_cached.clear()


def _model(model_id="model-a", marker=1):
    return {"model_id": model_id, "cells": [{"marker": marker}], "rows": []}


def _call(model, token, scale=22.0, detailed=True, labels=None, version=1):
    return app.build_geometry_html_cached(
        model, token, scale, detailed, labels or {"label_mode": "Авто"}, version
    )


def test_installed_streamlit_ignores_heavy_underscore_arguments(monkeypatch):
    calls = []

    def builder(model, **kwargs):
        calls.append((copy.deepcopy(model), copy.deepcopy(kwargs["label_settings"])))
        return str(model["cells"][0]["marker"])

    monkeypatch.setattr(app, "build_geometry_html", builder)
    token = ("model-a", 0, 0, 0, 0)
    assert _call(_model(marker=1), token, labels={"large": [1] * 1000}) == "1"
    assert _call(_model(marker=2), token, labels={"large": [2] * 1000}) == "1"
    assert len(calls) == 1


def test_cache_miss_uses_current_payload_without_mutating_it(monkeypatch):
    original = _model(marker=7)
    labels = {"label_mode": "Полные", "nested": {"value": 1}}
    expected_model = copy.deepcopy(original)
    expected_labels = copy.deepcopy(labels)

    def mutating_builder(model, **kwargs):
        model["cells"][0]["marker"] = 99
        kwargs["label_settings"]["nested"]["value"] = 99
        return "fresh"

    monkeypatch.setattr(app, "build_geometry_html", mutating_builder)
    assert _call(original, ("model-a", 1, 0, 0, 0), labels=labels) == "fresh"
    assert original == expected_model
    assert labels == expected_labels


@pytest.mark.parametrize("domain", app.GEOMETRY_RENDER_DOMAINS)
def test_each_render_domain_revision_causes_one_rebuild(monkeypatch, domain):
    calls = []
    monkeypatch.setattr(
        app,
        "build_geometry_html",
        lambda model, **kwargs: calls.append(model) or str(len(calls)),
    )
    model = _model()
    base = app.get_geometry_render_revision_token(model)
    assert _call(_model(), base) == "1"
    revisions.bump_revisions("model-a", [domain], "test")
    changed = app.get_geometry_render_revision_token(model)
    assert _call(_model(), changed) == "2"
    assert _call(_model(), changed) == "2"
    assert len(calls) == 2


def test_unrelated_revision_does_not_change_render_token(tmp_path):
    before = app.get_geometry_render_revision_token(_model())
    revisions.bump_revisions("model-a", ["receipts", "inventory"], "unrelated")
    assert app.get_geometry_render_revision_token(_model()) == before


@pytest.mark.parametrize(
    "override",
    [
        {"scale": 23.0},
        {"detailed": False},
        {"version": app.GEOMETRY_HTML_CACHE_VERSION + 1},
    ],
)
def test_small_view_key_changes_cause_cache_miss(monkeypatch, override):
    calls = []
    monkeypatch.setattr(app, "build_geometry_html", lambda model, **kwargs: calls.append(1) or str(len(calls)))
    token = ("model-a", 0, 0, 0, 0)
    assert _call(_model(), token) == "1"
    assert _call(_model(), token, **override) == "2"


def test_model_id_is_in_token_and_prevents_cross_model_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "build_geometry_html", lambda model, **kwargs: calls.append(1) or model["model_id"])
    first = app.get_geometry_render_revision_token(_model("model-a"))
    second = app.get_geometry_render_revision_token(_model("model-b"))
    assert first[0] == "model-a" and second[0] == "model-b"
    assert _call(_model("model-a"), first) == "model-a"
    assert _call(_model("model-b"), second) == "model-b"
    assert len(calls) == 2


def test_missing_revision_file_is_side_effect_free():
    assert not revisions.REVISION_PATH.exists()
    assert app.get_geometry_render_revision_token(_model()) == ("model-a", 0, 0, 0, 0)
    assert not revisions.REVISION_PATH.exists()


def test_corrupt_revision_state_bypasses_cached_wrapper(monkeypatch):
    revisions.REVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    revisions.REVISION_PATH.write_text("not json", encoding="utf-8")
    state = revisions.load_revision_state("model-a")
    assert state["warning"]
    # The view's branch must call the builder directly rather than manufacture a zero key.
    source = Path(app.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    view = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_geometry_map_view")
    assert "geometry_token is None" in ast.unparse(view)
    assert "build_geometry_html(copy.deepcopy(model)" in ast.unparse(view)


def test_wrapper_and_call_site_do_not_serialize_heavy_payloads():
    source = Path(app.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    wrapper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_geometry_html_cached")
    assert [arg.arg for arg in wrapper.args.args][:1] == ["_model_payload"]
    assert "_label_settings" in [arg.arg for arg in wrapper.args.args]
    wrapper_source = ast.unparse(wrapper)
    assert "json.loads" not in wrapper_source
    view = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_geometry_map_view")
    view_source = ast.unparse(view)
    assert "json.dumps(model" not in view_source
    assert "st.cache_data.clear" not in source


def test_cache_hit_does_not_build_or_deepcopy(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "build_geometry_html", lambda model, **kwargs: calls.append(1) or "html")
    token = ("model-a", 0, 0, 0, 0)
    assert _call(_model(), token) == "html"
    monkeypatch.setattr(app.copy, "deepcopy", lambda value: pytest.fail("cache hit copied payload"))
    assert _call(_model(marker=999), token, labels={"changed": True}) == "html"
    assert len(calls) == 1


def test_invalidation_still_clears_both_geometry_caches(monkeypatch, tmp_path):
    cleared = []
    monkeypatch.setattr(app, "RENDER_CACHE_PATH", tmp_path / "render_cache.json")
    app.RENDER_CACHE_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app.build_geometry_html_cached, "clear", lambda: cleared.append("html"))
    monkeypatch.setattr(app.prepare_render_cache_cached, "clear", lambda: cleared.append("render"))
    app.invalidate_geometry_render_cache()
    assert cleared == ["html", "render"]
    assert not app.RENDER_CACHE_PATH.exists()
