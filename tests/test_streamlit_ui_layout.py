import ast
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

APP_PATH = Path(__file__).resolve().parents[1] / "virtual_warehouse_app.py"
sys.path.insert(0, str(APP_PATH.parent))

import virtual_warehouse_app as app


SOURCE = APP_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function_source(name: str) -> str:
    node = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.get_source_segment(SOURCE, node) or ""


def test_geometry_mode_has_dedicated_settings_tab():
    body = _function_source("render_excel_geometry_warehouse")

    for label in ("Карта склада", "Настройки склада", "Приходы и инвент", "Аналитика", "Служебное"):
        assert f'"{label}"' in body
    assert "render_warehouse_map_tab(model)" in body
    assert "render_warehouse_settings_tab(model)" in body
    assert "render_receipts_inventory_tab(model)" in body
    assert "render_analytics_fragment(model)" in body
    assert "render_service_tab(saved_model, model)" in body
    assert "st.tabs(" not in body
    assert 'key="warehouse_active_section"' in body
    assert "horizontal=True" in body


class _SectionNavigationStreamlit:
    def __init__(self, selected=None):
        self.session_state = {}
        if selected is not None:
            self.session_state["warehouse_active_section"] = selected

    def title(self, _label):
        pass

    def caption(self, _label):
        pass

    def radio(self, _label, options, *, format_func, horizontal, key):
        assert horizontal is True
        assert [format_func(option) for option in options] == [
            "Карта склада",
            "Настройки склада",
            "Приходы и инвент",
            "Аналитика",
            "Служебное",
        ]
        self.session_state.setdefault(key, options[0])
        return self.session_state[key]


@pytest.mark.parametrize(
    ("selected", "expected_renderer", "expected_step"),
    [
        (None, "map", "render_section_map"),
        ("settings", "settings", "render_section_settings"),
        ("receipts_inventory", "receipts_inventory", "render_section_receipts_inventory"),
        ("analytics", "analytics", "render_section_analytics"),
        ("service", "service", "render_section_service"),
    ],
)
def test_only_selected_warehouse_section_is_rendered(monkeypatch, selected, expected_renderer, expected_step):
    fake_st = _SectionNavigationStreamlit(selected)
    calls = []
    steps = []

    @contextmanager
    def record_step(name):
        steps.append(name)
        yield

    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "load_geometry_model", lambda: {"model_id": "saved"})
    monkeypatch.setattr(app, "measure_step", record_step)
    monkeypatch.setattr(app, "render_warehouse_map_tab", lambda model: calls.append("map"))
    monkeypatch.setattr(app, "render_warehouse_settings_tab", lambda model: calls.append("settings"))
    monkeypatch.setattr(app, "render_receipts_inventory_tab", lambda model: calls.append("receipts_inventory"))
    monkeypatch.setattr(app, "render_analytics_fragment", lambda model: calls.append("analytics"))
    monkeypatch.setattr(app, "render_service_tab", lambda saved_model, model: calls.append("service"))

    app.render_excel_geometry_warehouse()

    assert calls == [expected_renderer]
    assert steps == ["load_geometry_model", expected_step]
    assert fake_st.session_state["warehouse_active_section"] == (selected or "map")


def test_inactive_settings_renderer_error_does_not_break_map(monkeypatch):
    fake_st = _SectionNavigationStreamlit("map")
    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "load_geometry_model", lambda: {})
    monkeypatch.setattr(app, "render_warehouse_map_tab", lambda model: None)
    monkeypatch.setattr(
        app,
        "render_warehouse_settings_tab",
        lambda model: (_ for _ in ()).throw(RuntimeError("settings renderer called")),
    )

    app.render_excel_geometry_warehouse()


def test_geometry_screen_loads_applied_model_without_row_settings_sync():
    body = _function_source("render_excel_geometry_warehouse")

    assert "saved_model = load_geometry_model()" in body
    assert "sync_row_settings_to_model" not in body
    assert 'st.session_state["geometry_model"] = saved_model' in body


def test_row_and_zone_settings_are_separate_from_map_view():
    map_body = _function_source("render_warehouse_map_tab")
    body = _function_source("render_warehouse_settings_tab")

    assert "render_row_settings_fragment(model)" in body
    assert "render_aisles_fragment" in body
    assert "render_zone_boundaries_fragment" in body
    assert "render_unified_row_settings_editor" not in map_body
    assert "render_map_edit_panel" not in map_body


def test_receipts_and_inventory_are_only_in_operations_tab_renderer():
    operations = _function_source("render_receipts_inventory_tab")
    analytics = _function_source("render_analytics_tab")
    map_tab = _function_source("render_warehouse_map_tab")

    assert "render_receipts_fragment(model)" in operations
    assert "render_inventory_fragment(model)" in operations
    assert "render_receipts_section(model)" not in analytics + map_tab
    assert "render_inventory_placement(model)" not in analytics + map_tab
    assert SOURCE.count("render_receipts_section(model)") == 1
    assert SOURCE.count("render_inventory_placement(model)") == 1


def test_destructive_reset_is_confined_to_service_and_confirmed():
    service = _function_source("render_service_tab")
    top_level = _function_source("render_excel_geometry_warehouse")

    assert '"Опасные действия"' in service
    assert '"Подтверждаю полный сброс проекта"' in service
    assert '"Полный сброс проекта"' in service
    assert "disabled=not reset_confirm" in service
    assert "clear_placement_state()" not in top_level


def test_workflow_uses_add_and_carryover_action_labels():
    receipts = _function_source("render_receipts_section")
    inventory = _function_source("render_inventory_placement")

    assert '"Добавить приход на текущий склад"' in receipts
    assert '"Зафиксировать переходящий остаток"' in inventory
    assert "reconcile_placements_with_inventory" in inventory
    assert "save_placement_state(reconciled_state)" in inventory
    assert SOURCE.count("calculate_basic_weight_placement(model, placement_state, state)") == 1


def test_map_exposes_only_working_cell_editor_controls():
    editor = _function_source("render_map_edit_panel")

    assert '"Ручное редактирование ячеек"' in editor
    assert "Выбрана ячейка" in editor
    assert '"Применить изменения ячейки"' in editor
    assert '"Добавить до"' in editor
    assert '"Добавить после"' in editor
    assert '"Подтвердить удаление ячейки"' in editor
    assert '"Режим редактирования"' not in editor
    assert '"Перемещение"' not in editor
    assert '"Выделение рамкой"' not in editor
    assert '"+ Ячейка"' not in editor
    assert '"− Ячейка"' not in editor
    assert "render_bulk_map_actions" not in editor


def test_row_shift_is_only_exposed_as_experimental_service_action():
    service = _function_source("render_service_tab")
    map_editor = _function_source("render_map_edit_panel")

    assert '"Экспериментальное редактирование геометрии"' in service
    assert "Ручной сдвиг может нарушить геометрию проездов" in service
    assert "_shift_rows" in service
    assert "_shift_rows" not in map_editor


def test_row_offsets_are_editable_in_the_single_draft_table():
    editor = _function_source("render_unified_row_settings_editor")

    assert "Отступ сверху, ячеек" in SOURCE
    assert "Отступ снизу, ячеек" in SOURCE
    assert '"Отступ сверху, ячеек"' in editor
    assert '"Отступ снизу, ячеек"' in editor


def test_map_renderer_is_view_only_and_row_editor_uses_a_form():
    map_view = _function_source("render_geometry_map_view")
    editor = _function_source("render_unified_row_settings_editor")

    assert "render_map_edit_panel" not in map_view
    assert "st.form(" in editor
    assert 'form_submit_button("Применить изменения"' in editor
    assert 'form_submit_button("Отменить изменения"' in editor
    assert "apply_row_settings_transaction" in editor
    assert "on_change=" not in editor


def test_outbound_picking_is_available_by_the_map_with_guarded_actions():
    map_tab = _function_source("render_warehouse_map_tab")
    picking = _function_source("render_outbound_picking")

    assert '"Моделирование сборки"' in map_tab
    assert "render_outbound_fragment(model)" in map_tab
    assert '"Собрать выбранные РО"' in picking
    assert '"Собрать все необработанные РО"' in picking
    assert '"Сбросить результаты сборки"' in picking
    assert "disabled=not selected_keys" in picking
    assert "disabled=not all_unprocessed" in picking


class _SubsectionStreamlit:
    def __init__(self, key, selected=None):
        self.session_state = {}
        if selected is not None:
            self.session_state[key] = selected

    def radio(self, _label, options, *, format_func, horizontal, key, label_visibility):
        assert horizontal is True
        assert label_visibility == "collapsed"
        assert all(format_func(option) for option in options)
        self.session_state.setdefault(key, options[0])
        return self.session_state[key]

    def subheader(self, _label):
        pass

    def caption(self, _label):
        pass

    def info(self, _label):
        pass

    @contextmanager
    def expander(self, _label):
        yield


@pytest.mark.parametrize(
    ("selected", "expected_renderer", "expected_step"),
    [
        (None, "geometry", "render_subsection_map_geometry"),
        ("outbound", "outbound", "render_subsection_map_outbound"),
    ],
)
def test_only_selected_map_subsection_is_rendered(monkeypatch, selected, expected_renderer, expected_step):
    fake_st = _SubsectionStreamlit("warehouse_map_subsection", selected)
    calls = []
    steps = []

    @contextmanager
    def record_step(name):
        steps.append(name)
        yield

    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "measure_step", record_step)
    monkeypatch.setattr(app, "render_map_geometry_fragment", lambda model: calls.append("geometry"))
    monkeypatch.setattr(app, "render_outbound_fragment", lambda model: calls.append("outbound"))

    app.render_warehouse_map_tab({"model_id": "model"})

    assert calls == [expected_renderer]
    assert steps == [expected_step]
    assert fake_st.session_state["warehouse_map_subsection"] == (selected or "map")


@pytest.mark.parametrize(
    ("selected", "expected_renderer", "expected_step"),
    [
        (None, "rows", "render_subsection_settings_rows"),
        ("cross_aisles", "cross_aisles", "render_subsection_settings_cross_aisles"),
        ("aisles", "aisles", "render_subsection_settings_aisles"),
        ("zones", "zones", "render_subsection_settings_zones"),
    ],
)
def test_only_selected_settings_subsection_is_rendered(monkeypatch, selected, expected_renderer, expected_step):
    fake_st = _SubsectionStreamlit("warehouse_settings_subsection", selected)
    calls = []
    steps = []

    @contextmanager
    def record_step(name):
        steps.append(name)
        yield

    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "measure_step", record_step)
    monkeypatch.setattr(app, "render_row_settings_fragment", lambda model: calls.append("rows"))
    monkeypatch.setattr(app, "render_cross_aisles_fragment", lambda model: calls.append("cross_aisles"))
    monkeypatch.setattr(app, "render_aisles_fragment", lambda model: calls.append("aisles"))
    monkeypatch.setattr(app, "render_zone_boundaries_fragment", lambda model: calls.append("zones"))

    app.render_warehouse_settings_tab({"model_id": "model"})

    assert calls == [expected_renderer]
    assert steps == [expected_step]
    assert fake_st.session_state["warehouse_settings_subsection"] == (selected or "rows")


@pytest.mark.parametrize(
    ("selected", "expected_renderer", "expected_step"),
    [
        (None, "receipts", "render_subsection_receipts"),
        ("inventory", "inventory", "render_subsection_inventory"),
        ("history", "history", "render_subsection_history"),
    ],
)
def test_only_selected_receipts_subsection_is_rendered(monkeypatch, selected, expected_renderer, expected_step):
    fake_st = _SubsectionStreamlit("warehouse_receipts_subsection", selected)
    calls = []
    steps = []

    @contextmanager
    def record_step(name):
        steps.append(name)
        yield

    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "measure_step", record_step)
    monkeypatch.setattr(app, "render_receipts_fragment", lambda model: calls.append("receipts"))
    monkeypatch.setattr(app, "render_inventory_fragment", lambda model: calls.append("inventory"))
    monkeypatch.setattr(app, "render_operation_history_fragment", lambda model: calls.append("history"))

    app.render_receipts_inventory_tab({"model_id": "model"})

    assert calls == [expected_renderer]
    assert steps == [expected_step]
    assert fake_st.session_state["warehouse_receipts_subsection"] == (selected or "receipts")


def test_inactive_nested_renderer_error_does_not_break_active_subsection(monkeypatch):
    fake_st = _SubsectionStreamlit("warehouse_map_subsection", "map")
    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "render_map_geometry_fragment", lambda model: None)
    monkeypatch.setattr(
        app,
        "render_outbound_fragment",
        lambda model: (_ for _ in ()).throw(RuntimeError("inactive renderer called")),
    )

    app.render_warehouse_map_tab({"model_id": "model"})


def test_nested_heavy_renderers_are_not_hidden_in_tabs_or_expanders():
    map_tab = _function_source("render_warehouse_map_tab")
    settings = _function_source("render_warehouse_settings_tab")
    operations = _function_source("render_receipts_inventory_tab")

    assert "st.tabs(" not in operations
    assert "st.expander(" not in map_tab + settings
