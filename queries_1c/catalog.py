"""Загрузка поставляемых вместе с приложением запросов 1С."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Query1C:
    slug: str
    title: str
    description: str
    filename: str
    text: str
    parameters: tuple[tuple[str, str], ...]
    result_columns: tuple[tuple[str, str], ...]


_QUERY_DIR = Path(__file__).resolve().parent


def load_query_catalog() -> tuple[Query1C, ...]:
    """Return the checked-in query catalog in its display order."""
    query_path = _QUERY_DIR / "mass_outbound_orders.query"
    return (
        Query1C(
            slug="mass_outbound_orders",
            title="Массовая выгрузка расходных ордеров (РО)",
            description=(
                "Выгружает строки товаров из расходных ордеров за период по выбранному "
                "складу. Запрос предназначен для запуска в консоли запросов 1С."
            ),
            filename=query_path.name,
            text=query_path.read_text(encoding="utf-8"),
            parameters=(
                ("НачалоПериода", "Начало периода отбора, включая указанную дату и время."),
                ("КонецПериода", "Конец периода отбора, включая указанную дату и время."),
                ("Склад", "Ссылка на склад, по которому выгружаются РО."),
                ("ТолькоПроведенные", "Булево: Истина — вернуть только проведённые документы."),
            ),
            result_columns=(
                ("НомерРО", "Номер расходного ордера."),
                ("ДатаРО", "Дата расходного ордера."),
                ("СтатусРО", "Статус документа."),
                ("Склад", "Склад расходного ордера."),
                ("Получатель", "Получатель (клиент) по документу."),
                ("КодТовара", "Код номенклатуры."),
                ("Товар", "Номенклатура из строки РО."),
                ("Характеристика", "Характеристика номенклатуры."),
                ("Серия", "Серия номенклатуры."),
                ("Упаковка", "Упаковка из строки РО."),
                ("Количество", "Количество к отбору в единицах строки."),
            ),
        ),
    )
