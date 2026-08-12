from warehouse_workspace_ui import (
    WORKSPACE_TABS, SUPPORTED_RULES, UNSUPPORTED_RULES, RULE_CARDS, MONTHLY_ROUTE_REQUIRED_SOURCES,
    WORKSPACE_PENDING_SECTION_KEY, apply_pending_workspace_navigation,
    build_warehouse_zone_summary, build_workspace_rule_config, normalize_rule_selection,
    build_data_source_cards, deep_lane_edit_issue, import_status_label,
    format_compact_number, format_monthly_readiness_blocker, format_monthly_readiness_check,
    monthly_readiness_blocker_details, monthly_readiness_message, status_card_html,
)
import warehouse_workspace_ui as workspace


def test_six_business_workspace_tabs_are_fixed():
    assert WORKSPACE_TABS == ("Настройка склада", "Загрузка данных", "Правила размещения",
                              "Сравнение вариантов", "Расчёт маршрутов", "Результаты")


class _WorkspaceStreamlit:
    def __init__(self, selected, click=False):
        self.session_state = {"workspace_section": selected}
        self.click = click
        self.widget_created = False

    def markdown(self, *args, **kwargs): pass
    def radio(self, _label, _options, **kwargs):
        self.widget_created = True
        return self.session_state["workspace_section"]
    def write(self, *_args, **_kwargs): pass
    def warning(self, *_args, **_kwargs): pass
    def success(self, *_args, **_kwargs): pass
    def button(self, *_args, **_kwargs):
        clicked, self.click = self.click, False
        return clicked


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


def test_next_action_defers_widget_bound_state_until_next_rerun(monkeypatch):
    fake = _WorkspaceStreamlit("Загрузка данных", click=True)
    monkeypatch.setattr(workspace, "st", fake)
    renderers = {key: (lambda _model: None) for key in (
        "warehouse_renderer", "data_renderer", "rules_renderer", "comparison_renderer",
        "distance_renderer", "analytics_renderer")}

    workspace.render_operational_workspace(None, **renderers)

    assert fake.widget_created
    assert fake.session_state["workspace_section"] == "Загрузка данных"
    assert fake.session_state[WORKSPACE_PENDING_SECTION_KEY] == "Правила размещения"

    apply_pending_workspace_navigation(fake.session_state)
    assert fake.session_state["workspace_section"] == "Правила размещения"
    assert WORKSPACE_PENDING_SECTION_KEY not in fake.session_state


def test_pending_navigation_is_applied_before_radio_and_only_once(monkeypatch):
    fake = _WorkspaceStreamlit("Загрузка данных")
    fake.session_state[WORKSPACE_PENDING_SECTION_KEY] = "Правила размещения"
    monkeypatch.setattr(workspace, "st", fake)
    selected = []
    original_radio = fake.radio

    def recording_radio(*args, **kwargs):
        selected.append(fake.session_state["workspace_section"])
        return original_radio(*args, **kwargs)

    fake.radio = recording_radio
    renderers = {key: (lambda _model: None) for key in (
        "warehouse_renderer", "data_renderer", "rules_renderer", "comparison_renderer",
        "distance_renderer", "analytics_renderer")}

    workspace.render_operational_workspace(None, **renderers)
    workspace.render_operational_workspace(None, **renderers)

    assert selected == ["Правила размещения", "Правила размещения"]


def test_unknown_pending_navigation_is_ignored_and_manual_selection_survives():
    state = {"workspace_section": "Результаты", WORKSPACE_PENDING_SECTION_KEY: "Неизвестный раздел"}

    apply_pending_workspace_navigation(state)

    assert state == {"workspace_section": "Результаты"}


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


def test_design_system_status_cards_are_consistent_and_escape_content():
    assert format_compact_number(643910) == "643 910"
    card = status_card_html("Расходные <ордера>", 643910, "Данные готовы", "success")
    assert "✅" in card and "Готово" in card and "643 910" in card
    assert "Расходные &lt;ордера&gt;" in card
    assert "ui-status-card success" in card


def test_unknown_status_uses_neutral_not_completed_presentation():
    card = status_card_html("Проверка качества", "Нет данных", "Запустите проверку", "unknown")
    assert "⬜" in card and "Не выполнено" in card
    assert "ui-status-card empty" in card


def test_incomplete_vgh_is_a_visible_warning_but_routes_remain_available():
    message = monthly_readiness_message({"monthly_replay_ready": True, "vgh_ready": False})
    rendered = format_monthly_readiness_check({"name": "vgh_coverage", "status": "warning", "title": "ВГХ",
        "details": "658 / 924 SKU", "missing_sku_count": 266, "percentage": 71.2})

    assert message["severity"] == "warning"
    assert message["title"] == "Данные июля готовы с ограничениями"
    assert "Можно считать маршруты, ABC, частоту и расстояния" in message["impact"]
    assert "тяжёлое/лёгкое" in message["impact"]
    assert rendered.startswith("⚠️ **ВГХ**")
    assert "vgh" not in MONTHLY_ROUTE_REQUIRED_SOURCES


def test_historical_cell_blocker_shows_unique_addresses_and_day_repetitions():
    blocker = {
        "code": "historical_cell_unresolved",
        "demand_relevant_cells": 137,
        "unique_source_cells": 10,
        "source_cell_preview": ["152-32", "152-34"],
    }

    details = monthly_readiness_blocker_details(blocker)
    rendered = format_monthly_readiness_blocker(blocker)

    assert details["title"] == "Исторические ячейки не сопоставлены с моделью склада"
    assert "Уникальных адресов: 10" in rendered
    assert "Повторений адресов по дням: 137" in rendered
    assert "`152-32`" in rendered
    assert "\nЧто сделать:" in rendered
    assert "\\n" not in rendered


def test_unknown_readiness_blocker_is_never_hidden():
    rendered = format_monthly_readiness_blocker({"code": "new_blocker"})

    assert "Неизвестная блокировка готовности" in rendered
    assert "`new_blocker`" in rendered
