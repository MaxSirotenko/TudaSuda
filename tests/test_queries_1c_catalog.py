from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from queries_1c import load_query_catalog


OUTBOUND_EXPECTED_PARAMETERS = ("ДатаНачала", "ДатаОкончания", "Склады")
OUTBOUND_EXPECTED_RESULT_COLUMNS = (
    "СсылкаРО", "НомерРО", "ДатаРО", "Склад", "РЦ", "НомерСтроки",
    "КодНоменклатуры", "Номенклатура", "КодХарактеристики", "Характеристика",
    "ДатаПроизводства", "ЕдиницаИзмерения", "Количество", "КоличествоВКоробке",
    "РасчетноеОтгруженоКоробок", "КонтрольРасчета",
)
INVENTORY_EXPECTED_PARAMETERS = ("Склады",)
INVENTORY_EXPECTED_RESULT_COLUMNS = (
    "Склад", "РЦ", "КодЯчейки", "Ячейка", "АдресЯчейки", "Ряд", "НомерЯчейки",
    "Ярус", "ПорядокСборки", "КодНоменклатуры", "Номенклатура",
    "КодХарактеристики", "Характеристика", "ДатаПроизводства", "ЕдиницаИзмерения",
    "Количество", "КоличествоПаллет", "КоличествоВКоробке",
    "РасчетноеКоличествоКоробов", "КонтрольРасчета",
)


def _names(items):
    return tuple(name for name, _description in items)


def test_catalog_contains_two_queries_in_display_order():
    catalog = load_query_catalog()

    assert isinstance(catalog, tuple)
    assert len(catalog) == 2
    assert tuple(query.slug for query in catalog) == (
        "mass_outbound_orders",
        "actual_inventory_by_cells",
    )


def test_catalog_exposes_complete_downloadable_outbound_query():
    query = load_query_catalog()[0]

    assert query.slug == "mass_outbound_orders"
    assert query.text.startswith("ВЫБРАТЬ")
    assert "Документ.РасходныйОрдерСклад.Товары" in query.text
    assert "РегистрСведений.КоличествоВКоробкеПоДатамПроизводства" in query.text
    assert "РасчетноеОтгруженоКоробок" in query.text
    assert _names(query.parameters) == OUTBOUND_EXPECTED_PARAMETERS
    assert _names(query.result_columns) == OUTBOUND_EXPECTED_RESULT_COLUMNS
    assert all(f"&{name}" in query.text for name, _ in query.parameters)
    assert all(name in query.text for name, _ in query.result_columns)
    assert "Документ.РасходныйОрдерНаТовары" not in query.text
    assert "&ТолькоПроведенные" not in query.text
    assert "СтрокиТоваров.Серия" not in query.text
    assert "СтрокиТоваров.Упаковка" not in query.text


def test_catalog_exposes_actual_inventory_query():
    query = load_query_catalog()[1]

    assert query.slug == "actual_inventory_by_cells"
    assert query.filename == "actual_inventory_by_cells.query"
    assert query.text.startswith("ВЫБРАТЬ")
    for fragment in (
        "РегистрНакопления.ПоложенияВЯчейках.Остатки",
        "РегистрНакопления.ТоварыНаПаллетах.Остатки",
        "РегистрСведений.КоличествоВКоробкеПоДатамПроизводства",
        "РасчетноеКоличествоКоробов",
        "КонтрольРасчета",
        "&Склады",
    ):
        assert fragment in query.text
    for period_parameter in ("&ДатаНачала", "&ДатаОкончания", "&НачалоПериода", "&КонецПериода"):
        assert period_parameter not in query.text
    assert _names(query.parameters) == INVENTORY_EXPECTED_PARAMETERS
    assert _names(query.result_columns) == INVENTORY_EXPECTED_RESULT_COLUMNS


def test_query_texts_are_loaded_from_separate_files():
    root = Path(__file__).parents[1] / "queries_1c"
    catalog = load_query_catalog()

    assert len({query.filename for query in catalog}) == len(catalog)
    for query in catalog:
        assert query.text == (root / query.filename).read_text(encoding="utf-8")
        assert query.text.endswith("\n")


def test_catalog_is_read_only_and_only_exposes_query_text():
    catalog = load_query_catalog()

    assert isinstance(catalog, tuple)
    assert all(not hasattr(query, "execute") for query in catalog)
    with pytest.raises(FrozenInstanceError):
        catalog[0].title = "Изменённое название"


def test_query_files_have_no_template_or_test_values():
    for query in load_query_catalog():
        assert "TODO" not in query.text
        assert "..." not in query.text
        assert "Тестовый товар" not in query.text
        assert "Тестовый склад" not in query.text
