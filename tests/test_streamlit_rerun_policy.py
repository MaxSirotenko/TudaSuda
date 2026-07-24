import ast
import importlib.util
from pathlib import Path
import sys

APP_PATH = Path(__file__).resolve().parents[1] / "virtual_warehouse_app.py"
SOURCE = APP_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _load_app():
    sys.path.insert(0, str(APP_PATH.parent))
    spec = importlib.util.spec_from_file_location("virtual_warehouse_app", APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function(name: str) -> ast.FunctionDef:
    return next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _source(name: str) -> str:
    return ast.get_source_segment(SOURCE, _function(name)) or ""


def test_local_fragment_interactions_have_no_explicit_rerun():
    analytics = _source("render_analytics_tab")
    rows = _source("render_unified_row_settings_editor")
    cross_aisles = _source("render_cross_aisle_settings_editor")

    assert "st.rerun" not in analytics
    assert rows.count("st.rerun()") == 1
    assert cross_aisles.count("st.rerun()") == 1
    assert 'st.info("Изменений нет")' in rows
    assert 'st.info("Изменений нет")' in cross_aisles


def test_reruns_are_not_scoped_or_used_by_fragment_wrappers():
    calls = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
        and node.func.attr == "rerun"
    ]
    assert calls
    assert all(not call.args and not call.keywords for call in calls)
    for name in (
        "render_row_settings_fragment",
        "render_cross_aisles_fragment",
        "render_analytics_fragment",
        "render_receipts_fragment",
        "render_inventory_fragment",
    ):
        assert "st.rerun" not in _source(name)


def test_geometry_cache_invalidation_is_targeted(monkeypatch, tmp_path):
    app = _load_app()
    cache_path = tmp_path / "render_cache.json"
    cache_path.write_text("{}", encoding="utf-8")
    cleared = []
    monkeypatch.setattr(app, "RENDER_CACHE_PATH", cache_path)
    monkeypatch.setattr(app.build_geometry_html_cached, "clear", lambda: cleared.append("geometry"))
    monkeypatch.setattr(app.prepare_render_cache_cached, "clear", lambda: cleared.append("render"))

    app.invalidate_geometry_render_cache()

    assert not cache_path.exists()
    assert cleared == ["geometry", "render"]


def test_no_global_streamlit_cache_clear_and_single_invalidation_site():
    assert "st.cache_data.clear" not in SOURCE
    assert SOURCE.count("RENDER_CACHE_PATH.unlink(") == 1
    assert SOURCE.count("build_geometry_html_cached.clear()") == 1
    assert SOURCE.count("prepare_render_cache_cached.clear()") == 1


def test_cancel_paths_do_not_rerun_or_invalidate_geometry_cache():
    rows = _source("render_unified_row_settings_editor")
    cross_aisles = _source("render_cross_aisle_settings_editor")
    assert "if reset_submit:" in rows
    assert "if cancel:" in cross_aisles
    assert rows.index("if reset_submit:") < rows.index("if apply_submit:")
    assert cross_aisles.index("if cancel:") < cross_aisles.index("if apply:")
    for source, cancel_marker, apply_marker in (
        (rows, "if reset_submit:", "if apply_submit:"),
        (cross_aisles, "if cancel:", "if apply:"),
    ):
        cancel_block = source[source.index(cancel_marker) : source.index(apply_marker)]
        assert "st.rerun" not in cancel_block
        assert "invalidate_geometry_render_cache" not in cancel_block
