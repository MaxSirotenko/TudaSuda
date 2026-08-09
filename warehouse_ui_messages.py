"""Central business-facing messages for operational workspace blockers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class UIMessage:
    severity: str
    title: str
    message: str
    solution: str
    target: str
    technical_code: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _m(code: str, title: str, message: str, solution: str, target: str, severity: str = "error") -> UIMessage:
    return UIMessage(severity, title, message, solution, target, code)


MESSAGE_CATALOG = {
    "missing_start": _m("missing_start", "Нет START", "Для CURRENT нет фактического остатка на начало дня.", 'Откройте Данные и загрузите "Остатки по ячейкам — НАЧАЛО дня".', "Данные / START"),
    "opening_stock_not_business_ready": _m("opening_stock_not_business_ready", "START нельзя использовать для расчёта", "В части исходного остатка не подтверждено физическое размещение.", "Откройте диагностику START и проверьте паллеты и ячейки с ошибками.", "Данные / START"),
    "missing_pallet_evidence": _m("missing_pallet_evidence", "Не для всех остатков подтверждена паллета", "Без идентификатора паллеты нельзя достоверно восстановить фактическое размещение.", 'Повторите актуальную выгрузку фактического размещения, содержащую существующее поле "Паллета".', "Данные / START"),
    "unknown_start_cell": _m("unknown_start_cell", "Ячейка из START отсутствует в модели склада", "Остаток нельзя привязать к физическому месту.", "Проверьте номер ряда/ячейки в модели склада и в исходной выгрузке.", "Склад / Настройки рядов"),
    "factual_pick_sequence_missing_or_invalid": _m("factual_pick_sequence_missing_or_invalid", "Нет достоверного порядка сборки", "Без фактической последовательности маршрут РО не будет авторитетным.", 'Используйте актуальную выгрузку РО с существующим полем "ПорядокСборки".', "Данные / РО"),
    "gate_missing": _m("gate_missing", "Не настроены ворота", "Неизвестна точка начала и возврата сборщика.", "Откройте Склад → Ворота и выберите точку начала и возврата сборщика.", "Склад / Ворота"),
    "warehouse_mismatch": _m("warehouse_mismatch", "Склады START и РО не совпадают", "Нельзя сравнивать размещение и спрос разных складов.", "Выберите одинаковый склад или загрузите РО соответствующего склада.", "Данные / РО"),
    "operational_date_without_orders": _m("operational_date_without_orders", "На выбранную дату нет РО", "Для операционного дня в загруженной выгрузке нет расходных РО.", "Выберите день, для которого в загруженной выгрузке есть РО.", "Данные / Операционный день"),
    "outbound_rows_not_supplied": _m("outbound_rows_not_supplied", "Не загружены расходные РО", "Для V1 нет авторитетного набора расходных ордеров.", "В разделе Данные загрузите выгрузку РО с датой, складом и ПорядкомСборки.", "Данные / РО"),
    "inventory_control_supplied_but_no_valid_rows": _m("inventory_control_supplied_but_no_valid_rows", "Инвентаризация загружена, но в ней нет пригодных строк", "Необязательный контроль был явно предоставлен, однако ни одна строка не прошла проверку.", "Исправьте файл инвентаризации или удалите необязательный файл и продолжите без него.", "Данные / Inventory control"),
    "selected_operational_date_has_no_accepted_outbound_orders": _m("selected_operational_date_has_no_accepted_outbound_orders", "На выбранный день нет принятых РО", "В выбранном складе нет валидных расходных ордеров этого дня.", "Выберите дату из списка, сформированного по принятым РО, либо загрузите нужные РО.", "Данные / Операционный день"),
    "start_outbound_warehouse_scope_mismatch": _m("start_outbound_warehouse_scope_mismatch", "Склад START не совпадает со складом РО", "Фактический START и расходный спрос относятся к разным складам.", "Загрузите РО выбранного склада или выберите соответствующий склад START.", "Данные / Склад"),
    "proposed_not_ready": _m("proposed_not_ready", "PROPOSED не построен", "Оптимизатор не сформировал готовое размещение для выбранного RuleSet.", "Откройте CURRENT / PROPOSED, исправьте показанные блокеры и пересчитайте PROPOSED.", "CURRENT / PROPOSED"),
    "exactly_one_mapped_gate_required": _m("exactly_one_mapped_gate_required", "Не выбраны одни валидные ворота", "Маршрут требует одну настроенную точку начала и возврата.", "В Склад → Ворота настройте ворота; если их несколько, выберите одни в Данные.", "Склад / Ворота"),
    "replenishment_requires_picking_storage": _m("replenishment_requires_picking_storage", "Пополнение требует Комплектации / хранения", "Пополнение не имеет смысла без разделения позиций комплектации и хранения.", 'Включите правило "Комплектация / хранение".', "Условия модели"),
    "deep_width_on_normal_row": _m("deep_width_on_normal_row", "Набивная вместимость не применяется", "У обычного ряда нет глубины набивного хранения.", 'Сначала измените тип ряда на "Набивной ряд".', "Склад / Настройки рядов"),
    "deep_access_on_normal_row": _m("deep_access_on_normal_row", "Сторона доступа не применяется", "У обычного ряда не настраивается сторона доступа набивного хранения.", 'Сначала измените тип ряда на "Набивной ряд".', "Склад / Настройки рядов"),
    "deep_access_missing": _m("deep_access_missing", "Не настроена сторона доступа набивного ряда", "Без стороны доступа нельзя однозначно связать набивной ряд с маршрутом.", 'В настройках ряда выберите "Слева" или "Справа".', "Склад / Настройки рядов"),
    "proposed_stale": _m("proposed_stale", "PROPOSED устарел", "Геометрия, данные или условия модели изменились после расчёта.", "Пересчитать PROPOSED.", "CURRENT / PROPOSED"),
    "benchmark_stale": _m("benchmark_stale", "Расчёт пробега устарел", "Результат больше не соответствует текущему PROPOSED или спросу.", "Пересчитать пробег.", "CURRENT / PROPOSED / Пробег"),
    "service_mismatch": _m("service_mismatch", "Объём выполненного отбора отличается", "Экономия полного дня недостоверна при разном количестве собранных коробов.", "Проверьте shortage CURRENT и PROPOSED, устраните нехватку и пересчитайте пробег.", "CURRENT / PROPOSED / Пробег"),
}

ALIASES = {
    "opening_stock_missing": "missing_start", "missing_gate": "gate_missing",
    "full_day_service_equivalence_failed": "service_mismatch",
}


def get_ui_message(code: str, **context: Any) -> dict[str, str]:
    """Return actionable copy; unknown technical codes are still never primary text."""
    key = ALIASES.get(code, code)
    item = MESSAGE_CATALOG.get(key) or _m(code, "Расчёт пока недоступен", "Не выполнено обязательное условие следующего шага.", "Откройте Технические детали, затем исправьте указанное условие и повторите действие.", "Технические детали")
    result = item.to_dict()
    for field in ("title", "message", "solution"):
        try:
            result[field] = result[field].format(**context)
        except (KeyError, ValueError):
            pass
    return result


def group_ui_issues(code: str, details: list[Any], *, visible: int = 5) -> dict[str, Any]:
    return {"message": get_ui_message(code), "count": len(details), "visible_details": list(details[:visible]),
            "hidden_count": max(0, len(details) - visible), "all_details": list(details)}
