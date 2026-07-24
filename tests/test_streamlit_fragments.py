import ast
from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "virtual_warehouse_app.py"
SOURCE = APP_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

FRAGMENTS = {
    "render_map_geometry_fragment": ("fragment_map_geometry", "render_geometry_map_view"),
    "render_outbound_fragment": ("fragment_map_outbound", "render_outbound_picking"),
    "render_row_settings_fragment": ("fragment_settings_rows", "render_unified_row_settings_editor"),
    "render_cross_aisles_fragment": ("fragment_settings_cross_aisles", "render_cross_aisle_settings_editor"),
    "render_aisles_fragment": ("fragment_settings_aisles", "render_active_model_aisle_editor"),
    "render_zone_boundaries_fragment": ("fragment_settings_zones", "render_zone_boundaries_editor"),
    "render_receipts_fragment": ("fragment_receipts", "render_receipts_section"),
    "render_inventory_fragment": ("fragment_inventory", "render_inventory_placement"),
    "render_operation_history_fragment": ("fragment_history", "render_operation_history"),
    "render_analytics_fragment": ("fragment_analytics", "render_analytics_tab"),
}


def _function(name: str) -> ast.FunctionDef:
    return next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _source(name: str) -> str:
    return ast.get_source_segment(SOURCE, _function(name)) or ""


def test_active_subsection_wrappers_are_fragments_with_instrumentation():
    for name, (step, renderer) in FRAGMENTS.items():
        node = _function(name)
        assert any(ast.unparse(decorator) == "st.fragment" for decorator in node.decorator_list)
        body = _source(name)
        assert f'measure_step("{step}")' in body
        assert f"{renderer}(model)" in body


def test_navigation_remains_outside_fragments():
    top_level = _source("render_excel_geometry_warehouse")
    map_section = _source("render_warehouse_map_tab")
    settings_section = _source("render_warehouse_settings_tab")
    receipts_section = _source("render_receipts_inventory_tab")

    assert 'key="warehouse_active_section"' in top_level
    assert 'key="warehouse_map_subsection"' in map_section
    assert 'key="warehouse_settings_subsection"' in settings_section
    assert 'key="warehouse_receipts_subsection"' in receipts_section
    assert all("st.radio(" not in _source(name) for name in FRAGMENTS)


def test_fragments_do_not_use_external_containers_or_scoped_reruns():
    for name in FRAGMENTS:
        body = _source(name)
        assert "st.container(" not in body
        assert 'scope="fragment"' not in body


def test_installed_streamlit_runs_fragment_with_interactive_widget():
    app = AppTest.from_string(
        """
import streamlit as st

st.session_state.main_runs = st.session_state.get("main_runs", 0) + 1

@st.fragment
def editor():
    st.session_state.fragment_runs = st.session_state.get("fragment_runs", 0) + 1
    st.text_input("Draft", key="draft")
    st.write(f"fragment={st.session_state.fragment_runs}")

editor()
st.write(f"main={st.session_state.main_runs}")
"""
    ).run()

    assert app.session_state.main_runs == 1
    assert app.session_state.fragment_runs == 1

    app.text_input(key="draft").set_value("changed").run()

    # AppTest.run always starts a complete script run; this check verifies that
    # the installed Streamlit version can execute and interact with a fragment.
    assert app.session_state.main_runs == 2
    assert app.session_state.fragment_runs == 2
