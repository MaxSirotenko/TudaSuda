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
INVENTORY_RESULTS_EXPECTED_PARAMETERS = ("ДатаНачала", "ДатаОкончания", "Склады")
INVENTORY_RESULTS_EXPECTED_COLUMNS = (
    "СсылкаИнвентаризации", "НомерИнвентаризации", "ДатаИнвентаризации", "Склад",
    "РЦ", "НомерСтроки", "КодНоменклатуры", "Номенклатура",
    "КодХарактеристики", "Характеристика", "ЕдиницаИзмерения",
    "ФактическоеКоличество", "УчетноеКоличество", "Расхождение",
    "КоличествоВКоробке", "РасчетноеКоличествоКоробов", "КонтрольРасчета",
)
DAY_RECEIPTS_EXPECTED_PARAMETERS = ("ДатаНачала", "ДатаОкончания", "Склады")
DAY_RECEIPTS_EXPECTED_COLUMNS = (
    "СсылкаПриходногоОрдера", "НомерПриходногоОрдера", "ДатаПриходногоОрдера",
    "Склад", "РЦ", "НомерСтроки", "КодНоменклатуры", "Номенклатура",
    "КодХарактеристики", "Характеристика", "КоличествоКоробок", "КоличествоПаллет",
    "ПриемкаТерминаломЗакончена", "ОжидаемыйПриход", "КонтрольКоличества",
)


def _names(items):
    return tuple(name for name, _description in items)


def test_catalog_contains_four_queries_in_display_order():
    catalog = load_query_catalog()

    assert isinstance(catalog, tuple)
    assert len(catalog) == 4
    assert tuple(query.slug for query in catalog) == (
        "mass_outbound_orders",
        "actual_inventory_by_cells",
        "inventory_results",
        "day_receipts",
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


def test_catalog_exposes_draft_inventory_results_query():
    query = load_query_catalog()[2]

    assert query.slug == "inventory_results"
    assert query.filename == "inventory_results.query"
    assert query.text.startswith("ВЫБРАТЬ")
    for fragment in (
        "Документ.ИнвентаризацияСклад.Товары",
        "РегистрСведений.КоличествоВКоробке.СрезПоследних",
        "ФактическоеКоличество",
        "УчетноеКоличество",
        "Расхождение",
        "РасчетноеКоличествоКоробов",
        "КонтрольРасчета",
        "&ДатаНачала",
        "&ДатаОкончания",
        "&Склады",
        "Ссылка.Проведен",
        "Ссылка.ПометкаУдаления",
    ):
        assert fragment in query.text
    assert (
        "ВТ_Инвентаризации.ФактическоеКоличество\n"
        "\t\t\t\t/ ВТ_КоличествоВКоробке.КоличествоВКоробке"
    ) in query.text
    for unrelated_source in (
        "ПоложенияВЯчейках",
        "ТоварыНаПаллетах",
        "РасходныйОрдерСклад",
        "ЗаданиеНаПриемку",
    ):
        assert unrelated_source not in query.text
    assert "МАКСИМУМ(\n\t\tВТ_Инвентаризации.ДатаИнвентаризации" not in query.text
    metadata = f"{query.title} {query.description}".lower()
    assert "чернов" in metadata
    assert "ручной провер" in metadata
    assert "проверен вручную" not in metadata
    assert "проверенный запрос" not in metadata
    assert _names(query.parameters) == INVENTORY_RESULTS_EXPECTED_PARAMETERS
    assert _names(query.result_columns) == INVENTORY_RESULTS_EXPECTED_COLUMNS


def test_catalog_exposes_draft_day_receipts_query():
    query = load_query_catalog()[3]

    assert query.slug == "day_receipts"
    assert query.filename == "day_receipts.query"
    assert query.text.startswith("ВЫБРАТЬ")
    for fragment in (
        "Документ.ПриходныйОрдерСклад.Товары",
        "КоличествоКоробок",
        "КоличествоПаллет",
        "ПриемкаТерминаломЗакончена",
        "ОжидаемыйПриход",
        "КонтрольКоличества",
        "&ДатаНачала",
        "&ДатаОкончания",
        "&Склады",
        "Ссылка.Проведен",
        "Ссылка.ПометкаУдаления",
    ):
        assert fragment in query.text
    assert "ПриходныйОрдерТовары.КоличествоКоробок КАК КоличествоКоробок" in query.text
    for forbidden_fragment in (
        "КоличествоВКоробке",
        "ВесИГабаритыУпаковки",
        "ЦЕЛ(",
        "РасчетноеКоличествоКоробов",
        "РасчетноеОтгруженоКоробок",
        "16:00",
        "ЧАС(",
        "ДОБАВИТЬКДАТЕ",
        "ВРЕМЯ(",
        "СГРУППИРОВАТЬ ПО",
        "МАКСИМУМ(",
        "СрезПоследних",
        "РасходныйОрдерСклад",
        "ИнвентаризацияСклад",
        "ПоложенияВЯчейках",
        "ТоварыНаПаллетах",
        "TODO",
        "...",
        "Тестовый товар",
        "Тестовый склад",
    ):
        assert forbidden_fragment not in query.text
    assert _names(query.parameters) == DAY_RECEIPTS_EXPECTED_PARAMETERS
    assert _names(query.result_columns) == DAY_RECEIPTS_EXPECTED_COLUMNS
    metadata = f"{query.title} {query.description}".lower()
    assert "чернов" in metadata
    assert "проверен вручную" not in metadata
    assert "проверенный запрос" not in metadata
    assert "количествокоробок" in metadata
    assert "16:00" in metadata
    assert "не группирует" in metadata
    assert "не выбирает последний" in metadata


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
