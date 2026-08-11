import pytest

from warehouse_ui_messages import MESSAGE_CATALOG, get_ui_message, group_ui_issues, render_ui_message


@pytest.mark.parametrize("code", [
    "missing_start", "missing_pallet_evidence", "unknown_start_cell",
    "factual_pick_sequence_missing_or_invalid", "gate_missing", "warehouse_mismatch",
    "operational_date_without_orders", "replenishment_requires_picking_storage",
    "deep_width_on_normal_row", "deep_access_missing", "proposed_stale",
    "benchmark_stale", "service_mismatch",
])
def test_blocking_messages_are_actionable_and_keep_technical_code_secondary(code):
    item = get_ui_message(code)
    assert item["severity"] == "error"
    assert all(item[key].strip() for key in ("title", "message", "solution", "technical_code"))
    assert item["technical_code"] == code
    assert code not in item["title"]


def test_unknown_code_has_safe_actionable_fallback():
    item = get_ui_message("configuration_errors")
    assert item["title"] != "configuration_errors" and item["solution"]


def test_repeated_errors_are_grouped():
    group = group_ui_issues("unknown_start_cell", list(range(17)))
    assert group["count"] == 17 and len(group["visible_details"]) == 5 and group["hidden_count"] == 12


def test_reusable_message_renders_all_actionable_sections_in_russian():
    class UI:
        def error(self, body): self.body = body
        def caption(self, _body): pass

    ui = UI()
    render_ui_message(get_ui_message("missing_start"), ui=ui)
    assert all(label in ui.body for label in ("Статус", "Причина", "Влияние", "Что делать"))
    assert "Ошибка" in ui.body
