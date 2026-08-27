from datetime import date, datetime

from warehouse_workspace_ui import (
    WORKSPACE_TABS, SUPPORTED_RULES, UNSUPPORTED_RULES, RULE_CARDS, MONTHLY_ROUTE_REQUIRED_SOURCES,
    WORKSPACE_PENDING_SECTION_KEY, apply_pending_workspace_navigation,
    build_warehouse_zone_summary, build_workspace_rule_config, normalize_rule_selection,
    build_data_source_cards, deep_lane_edit_issue, import_status_label, replacement_target_label,
    format_compact_number, format_monthly_readiness_blocker, format_monthly_readiness_check,
    format_ui_date, format_ui_period,
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


class _UploadedFile:
    def __init__(self, name="file.xlsx", payload=b"payload"):
        self.name = name
        self._payload = payload

    def getvalue(self):
        return self._payload


class _ProgressSlot:
    def __init__(self, ui):
        self.ui = ui

    def info(self, *args, **kwargs):
        self.ui.infos.append(args)

    def empty(self):
        self.ui.empty_called = True


class _FactualDataStreamlit:
    def __init__(self, *, uploads=None, clicked=None, selectbox_values=None):
        self.session_state = {}
        self.uploads = uploads or {}
        self.clicked = set(clicked or ())
        self.selectbox_values = selectbox_values or {}
        self.buttons = []
        self.selectboxes = []
        self.warnings = []
        self.infos = []
        self.successes = []
        self.empty_called = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def subheader(self, *_args, **_kwargs): pass
    def caption(self, *_args, **_kwargs): pass
    def markdown(self, *_args, **_kwargs): pass
    def write(self, *_args, **_kwargs): pass
    def json(self, *_args, **_kwargs): pass
    def success(self, *args, **_kwargs): self.successes.append(args)
    def warning(self, *args, **_kwargs): self.warnings.append(args)
    def info(self, *args, **_kwargs): self.infos.append(args)
    def error(self, *args, **_kwargs): pass
    def empty(self): return _ProgressSlot(self)
    def container(self, *_args, **_kwargs): return self
    def expander(self, *_args, **_kwargs): return self

    def file_uploader(self, _label, **kwargs):
        return self.uploads.get(kwargs.get("key"))

    def button(self, label, **kwargs):
        key = kwargs.get("key")
        disabled = bool(kwargs.get("disabled"))
        self.buttons.append({"label": label, "key": key, "disabled": disabled})
        return False if disabled else key in self.clicked or label in self.clicked

    def selectbox(self, label, options, **kwargs):
        self.selectboxes.append({"label": label, "options": list(options), "kwargs": kwargs})
        return self.selectbox_values.get(kwargs.get("key"))


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
    assert outbound["status"] == "✅ Загружено"
    assert outbound["load_status"] == "loaded"
    assert (outbound["warning_count"], outbound["error_count"]) == (0, 0)
    assert (outbound["documents"], outbound["rows"], outbound["sku"]) == (3, 12, 2)
    assert outbound["period"] == "01.07.2026 — 01.07.2026"
    assert outbound["dates"] == ["2026-07-01"]
    assert all(card["status"] == "⬜ Не загружено" for card in cards if card is not outbound)


def test_ui_single_source_replacement_passes_active_dataset_id(monkeypatch):
    registry = {"datasets": [{"active": True, "source_type": "outbound", "dataset_id": "target",
        "source_file_name": "РО июль.xlsx", "rows": 1, "warnings": [], "errors": [],
        "index": {"sku_keys": ["sku"], "dates": ["2026-07-15"], "daily": {"2026-07-15": {"rows": 1}}}}]}
    fake = _FactualDataStreamlit(
        uploads={"replace_outbound": _UploadedFile("РО новый.xlsx")},
        clicked={"replace_confirm_outbound"},
    )
    calls = []
    monkeypatch.setattr(workspace, "st", fake)
    monkeypatch.setattr(workspace, "load_registry", lambda: registry)
    monkeypatch.setattr(workspace, "replace_excel_dataset",
                        lambda data, name, dataset_id: calls.append((data, name, dataset_id))
                        or {"source_type": "outbound", "reused": False})

    workspace.render_factual_data_layer(None)

    assert calls == [(b"payload", "РО новый.xlsx", "target")]


def test_ui_multi_source_non_vgh_requires_target_selection(monkeypatch):
    registry = {"datasets": [
        {"active": True, "source_type": "outbound", "dataset_id": "a", "source_file_name": "РО A.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"dates": ["2026-07-15"], "daily": {}}},
        {"active": True, "source_type": "outbound", "dataset_id": "b", "source_file_name": "РО B.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"dates": ["2026-07-16"], "daily": {}}},
    ]}
    fake = _FactualDataStreamlit(
        uploads={"replace_outbound": _UploadedFile("РО новый.xlsx")},
        clicked={"replace_confirm_outbound"},
    )
    calls = []
    monkeypatch.setattr(workspace, "st", fake)
    monkeypatch.setattr(workspace, "load_registry", lambda: registry)
    monkeypatch.setattr(workspace, "replace_excel_dataset",
                        lambda *_args, **_kwargs: calls.append(_args) or {})

    workspace.render_factual_data_layer(None)

    assert fake.selectboxes and fake.selectboxes[0]["label"] == "Какой файл заменить"
    assert any(button["key"] == "replace_confirm_outbound" and button["disabled"]
               for button in fake.buttons)
    assert calls == []


def test_ui_multi_source_non_vgh_replaces_selected_target(monkeypatch):
    sources = [
        {"active": True, "source_type": "outbound", "dataset_id": "a", "source_file_name": "РО A.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"dates": ["2026-07-15"], "daily": {}}},
        {"active": True, "source_type": "outbound", "dataset_id": "b", "source_file_name": "РО B.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"dates": ["2026-07-16"], "daily": {}}},
    ]
    fake = _FactualDataStreamlit(
        uploads={"replace_outbound": _UploadedFile("РО новый.xlsx")},
        clicked={"replace_confirm_outbound"},
        selectbox_values={"replace_target_outbound": sources[1]},
    )
    calls = []
    monkeypatch.setattr(workspace, "st", fake)
    monkeypatch.setattr(workspace, "load_registry", lambda: {"datasets": sources})
    monkeypatch.setattr(workspace, "replace_excel_dataset",
                        lambda data, name, dataset_id: calls.append((data, name, dataset_id))
                        or {"source_type": "outbound", "reused": False})

    workspace.render_factual_data_layer(None)

    assert replacement_target_label(sources[1]).startswith("РО B.xlsx")
    assert calls == [(b"payload", "РО новый.xlsx", "b")]


def test_ui_multi_source_vgh_warns_and_does_not_require_target_selection(monkeypatch):
    registry = {"datasets": [
        {"active": True, "source_type": "vgh", "dataset_id": "a", "source_file_name": "ВГХ A.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"sku_keys": ["sku-a"], "dates": ["undated"], "daily": {}}},
        {"active": True, "source_type": "vgh", "dataset_id": "b", "source_file_name": "ВГХ B.xlsx",
         "rows": 1, "warnings": [], "errors": [], "index": {"sku_keys": ["sku-b"], "dates": ["undated"], "daily": {}}},
    ]}
    fake = _FactualDataStreamlit(
        uploads={"replace_vgh": _UploadedFile("ВГХ новый.xlsx")},
        clicked={"replace_confirm_vgh"},
    )
    calls = []
    monkeypatch.setattr(workspace, "st", fake)
    monkeypatch.setattr(workspace, "load_registry", lambda: registry)
    monkeypatch.setattr(workspace, "replace_excel_dataset",
                        lambda data, name, dataset_id: calls.append((data, name, dataset_id))
                        or {"source_type": "vgh", "reused": False})

    workspace.render_factual_data_layer(None)

    assert not fake.selectboxes
    assert any("несколько активных файлов ВГХ" in args[0] for args in fake.warnings)
    assert calls == [(b"payload", "ВГХ новый.xlsx", "a")]


def test_generic_upload_blocks_duplicate_vgh_only(monkeypatch):
    registry = {"datasets": [{"active": True, "source_type": "vgh", "dataset_id": "vgh",
        "source_file_name": "ВГХ.xlsx", "rows": 1, "warnings": [], "errors": [],
        "index": {"sku_keys": ["sku"], "dates": ["undated"], "daily": {}}}]}
    messages = []
    imports = []
    monkeypatch.setattr(workspace, "load_registry", lambda: registry)
    monkeypatch.setattr(workspace, "render_ui_message", lambda message: messages.append(message))

    duplicate_vgh = _FactualDataStreamlit(
        uploads={"factual_data_uploads": [_UploadedFile("ВГХ новая.xlsx")]},
        clicked={"factual_data_import"},
    )
    monkeypatch.setattr(workspace, "st", duplicate_vgh)
    monkeypatch.setattr(workspace, "detect_excel_dataset_source",
                        lambda *_args, **_kwargs: {"source_type": "vgh", "status": "detected"})
    monkeypatch.setattr(workspace, "import_excel_dataset",
                        lambda *_args, **_kwargs: imports.append(_args) or {})

    workspace.render_factual_data_layer(None)

    assert not imports
    assert messages and "ВГХ уже загружен" in messages[0]["reason"]

    outbound = _FactualDataStreamlit(
        uploads={"factual_data_uploads": [_UploadedFile("РО.xlsx")]},
        clicked={"factual_data_import"},
    )
    monkeypatch.setattr(workspace, "st", outbound)
    monkeypatch.setattr(workspace, "detect_excel_dataset_source",
                        lambda *_args, **_kwargs: {"source_type": "outbound", "status": "detected"})

    workspace.render_factual_data_layer(None)

    assert imports


def test_ui_date_formatter_is_strict_safe_and_presentation_only():
    iso_dates = ["2026-07-01", "2026-07-31"]

    assert format_ui_date(iso_dates[0]) == "01.07.2026"
    assert format_ui_date(iso_dates[1]) == "31.07.2026"
    assert format_ui_period(*iso_dates) == "01.07.2026 — 31.07.2026"
    assert format_ui_date(date(2026, 7, 1)) == "01.07.2026"
    assert format_ui_date(datetime(2026, 7, 31, 14, 30)) == "31.07.2026"
    assert format_ui_date(None) is None
    assert format_ui_date("") == ""
    assert format_ui_date("неизвестно") == "неизвестно"
    assert format_ui_date("2026-02-30") == "2026-02-30"
    assert iso_dates == ["2026-07-01", "2026-07-31"]


def test_readiness_and_blocker_dates_use_russian_ui_format():
    check = {"name": "placement_snapshot", "status": "fail", "details": "Период с 2026-07-01",
             "missing_dates": ["2026-07-01"], "extra_dates": ["2026-08-01"]}
    blocker = {"code": "missing_placement_snapshot", "dates": ["2026-07-01"]}

    rendered_check = format_monthly_readiness_check(check)
    rendered_blocker = format_monthly_readiness_blocker(blocker)
    assert "Период с 01.07.2026" in rendered_check
    assert "Отсутствующие даты: 01.07.2026" in rendered_check
    assert "Лишние даты: 01.08.2026" in rendered_check
    assert "Даты: 01.07.2026" in rendered_blocker
    assert check["missing_dates"] == ["2026-07-01"]
    assert blocker["dates"] == ["2026-07-01"]


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
