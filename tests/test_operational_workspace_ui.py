from warehouse_workspace_ui import (
    WORKSPACE_TABS, SUPPORTED_RULES, UNSUPPORTED_RULES, RULE_CARDS,
    build_warehouse_zone_summary, build_workspace_rule_config, normalize_rule_selection,
    build_data_source_cards, deep_lane_edit_issue, import_status_label,
)
import warehouse_workspace_ui as workspace


def test_six_business_workspace_tabs_are_fixed():
    assert WORKSPACE_TABS == ("Настройка склада", "Загрузка данных", "Правила размещения",
                              "Сравнение вариантов", "Расчёт маршрутов", "Результаты")


class _WorkspaceStreamlit:
    def __init__(self, selected):
        self.session_state = {"workspace_section": selected}

    def markdown(self, *args, **kwargs): pass
    def radio(self, _label, _options, **kwargs): return self.session_state["workspace_section"]
    def write(self, *_args, **_kwargs): pass
    def warning(self, *_args, **_kwargs): pass
    def success(self, *_args, **_kwargs): pass
    def button(self, *_args, **_kwargs): return False


def test_only_selected_workspace_section_executes(monkeypatch):
    fake = _WorkspaceStreamlit("Загрузка данных")
    monkeypatch.setattr(workspace, "st", fake)
    calls = []
    renderers = {key: (lambda _model, key=key: calls.append(key)) for key in (
        "warehouse_renderer", "data_renderer", "rules_renderer", "comparison_renderer",
        "distance_renderer", "analytics_renderer")}
    workspace.render_operational_workspace(None, **renderers)
    assert calls == ["data_renderer"]
    fake.session_state["workspace_section"] = "Результаты"
    calls.clear()
    workspace.render_operational_workspace(None, **renderers)
    assert calls == ["analytics_renderer"]


def test_only_supported_rules_are_active_and_dependency_is_deterministic():
    assert set(SUPPORTED_RULES).isdisjoint(UNSUPPORTED_RULES)
    a = normalize_rule_selection({"replenishment": True, "picking_storage": False})
    b = normalize_rule_selection({"picking_storage": False, "replenishment": True})
    assert a == b and not a["replenishment"]
    assert build_workspace_rule_config({"picking_storage": True, "replenishment": True})["replenishment"]["enabled"]


def test_rule_contract_has_only_real_parameter_and_exact_adjacency_copy():
    config = build_workspace_rule_config({"base_sku_capacity": True}, 2)
    assert config["base_sku_capacity"]["parameters"] == {"minimum_positions_per_sku": 2}
    assert config["deep_lane_optimization"] == {"enabled": False}
    assert RULE_CARDS["adjacency"][1] == "Разная номенклатура с одинаковой непустой характеристикой не размещается в соседних ячейках."


def test_every_workspace_rule_reaches_the_single_scenario_contract():
    config = build_workspace_rule_config({
        "weight_zones": True, "velocity": True, "adjacency": True,
        "picking_storage": True, "replenishment": True,
        "deep_lane_optimization": True, "base_sku_capacity": True,
    }, 4)
    assert all(config[name]["enabled"] for name in SUPPORTED_RULES)
    assert config["base_sku_capacity"]["parameters"]["minimum_positions_per_sku"] == 4


def test_zone_summary_uses_canonical_zone_ids_and_physical_capacity():
    model = {"cells": [
        {"row_number": 1, "weight_zone": "heavy", "storage_type": "normal", "capacity_pallets": 1},
        {"row_number": 2, "weight_zone": "medium_light", "storage_type": "deep_lane", "capacity_pallets": 4},
        {"row_number": 3, "weight_zone": "unknown", "storage_type": "normal", "capacity_pallets": 1},
    ]}
    rows = build_warehouse_zone_summary(model)
    assert [row["ID зоны"] for row in rows] == ["heavy", "medium_light", "unassigned"]
    assert rows[1]["Количество ячеек"] == 1 and rows[1]["Набивные места"] == 4


def test_normal_row_deep_controls_have_actionable_regression_message():
    width = deep_lane_edit_issue("normal", 5, "")
    access = deep_lane_edit_issue("normal", 1, "left")
    assert "Сначала измените тип ряда" in width["solution"]
    assert "Изменений нет" not in width["message"]
    assert "Сначала измените тип ряда" in access["solution"]
    assert deep_lane_edit_issue("deep_lane", 5, "left") is None


def test_import_statuses_are_presented_in_russian_without_changing_contract_values():
    assert import_status_label("ready") == "✅ Готово"
    assert import_status_label("ready_with_warnings") == "⚠️ Готово с ограничениями"
    assert import_status_label("unexpected_internal_status") == "❌ Требуется исправление"


def test_data_upload_cards_cover_five_sources_using_metadata_only():
    registry = {"datasets": [{
        "active": True, "source_type": "outbound", "source_file_name": "РО июль.xlsx",
        "rows": 12, "warnings": [], "errors": [],
        "index": {"sku_keys": ["A", "B"], "dates": ["2026-07-01"],
                  "daily": {"2026-07-01": {"rows": 12, "documents": 3, "cells": 0}}},
    }]}
    cards = build_data_source_cards(registry)
    assert [card["source_type"] for card in cards] == [
        "historical_placement", "outbound", "receipts", "inventory", "vgh"]
    outbound = next(card for card in cards if card["source_type"] == "outbound")
    assert outbound["file"] == "РО июль.xlsx"
    assert outbound["status"] == "✅ Готово"
    assert (outbound["documents"], outbound["rows"], outbound["sku"]) == (3, 12, 2)
    assert all(card["status"] == "⬜ Не загружено" for card in cards if card is not outbound)
