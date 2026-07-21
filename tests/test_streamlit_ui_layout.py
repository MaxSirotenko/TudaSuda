import ast
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "virtual_warehouse_app.py"
SOURCE = APP_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function_source(name: str) -> str:
    node = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.get_source_segment(SOURCE, node) or ""


def test_geometry_mode_has_four_top_level_tabs():
    body = _function_source("render_excel_geometry_warehouse")

    for label in ("Карта склада", "Приходы и инвент", "Аналитика", "Служебное"):
        assert f'"{label}"' in body
    assert "render_warehouse_map_tab(model)" in body
    assert "render_receipts_inventory_tab(model)" in body
    assert "render_analytics_tab(model)" in body
    assert "render_service_tab(saved_model, model)" in body


def test_row_and_zone_settings_are_rendered_with_the_map():
    body = _function_source("render_warehouse_map_tab")

    assert '"Настройки рядов и геометрии"' in body
    assert "render_unified_row_settings_editor(model)" in body
    assert "render_active_model_aisle_editor(model)" in body
    assert "render_zone_boundaries_editor(model)" in body


def test_receipts_and_inventory_are_only_in_operations_tab_renderer():
    operations = _function_source("render_receipts_inventory_tab")
    analytics = _function_source("render_analytics_tab")
    map_tab = _function_source("render_warehouse_map_tab")

    assert "render_receipts_section(model)" in operations
    assert "render_inventory_placement(model)" in operations
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


def test_row_offsets_are_editable_and_bulk_zero_requires_explicit_opt_in():
    editor = _function_source("render_unified_row_settings_editor")

    assert "Отступ сверху, ячеек" in SOURCE
    assert "Отступ снизу, ячеек" in SOURCE
    assert '"Изменить отступ сверху"' in editor
    assert '"Изменить отступ снизу"' in editor
    assert "if apply_top_offset:" in editor
    assert "if apply_bottom_offset:" in editor


def test_outbound_picking_is_available_by_the_map_with_guarded_actions():
    map_tab = _function_source("render_warehouse_map_tab")
    picking = _function_source("render_outbound_picking")

    assert '"Моделирование сборки"' in map_tab
    assert "render_outbound_picking(model)" in map_tab
    assert '"Собрать выбранные РО"' in picking
    assert '"Собрать все необработанные РО"' in picking
    assert '"Сбросить результаты сборки"' in picking
    assert "disabled=not selected_keys" in picking
    assert "disabled=not all_unprocessed" in picking
