from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from queries_1c import load_query_catalog


EXPECTED_PARAMETERS = ("ДатаНачала", "ДатаОкончания", "Склады")
EXPECTED_RESULT_COLUMNS = (
    "СсылкаРО",
    "НомерРО",
    "ДатаРО",
    "Склад",
    "РЦ",
    "НомерСтроки",
    "КодНоменклатуры",
    "Номенклатура",
    "КодХарактеристики",
    "Характеристика",
    "ДатаПроизводства",
    "ЕдиницаИзмерения",
    "Количество",
    "КоличествоВКоробке",
    "РасчетноеОтгруженоКоробок",
    "КонтрольРасчета",
)


def test_catalog_exposes_complete_downloadable_outbound_query():
    catalog = load_query_catalog()

    assert len(catalog) == 1
    query = catalog[0]
    assert query.slug == "mass_outbound_orders"
    assert query.text.startswith("ВЫБРАТЬ")
    assert "Документ.РасходныйОрдерСклад.Товары" in query.text
    assert "РегистрСведений.КоличествоВКоробкеПоДатамПроизводства" in query.text
    assert "РасчетноеОтгруженоКоробок" in query.text
    assert tuple(name for name, _description in query.parameters) == EXPECTED_PARAMETERS
    assert tuple(name for name, _description in query.result_columns) == EXPECTED_RESULT_COLUMNS
    assert all(f"&{name}" in query.text for name, _description in query.parameters)
    assert all(name in query.text for name, _description in query.result_columns)

    assert "Документ.РасходныйОрдерНаТовары" not in query.text
    assert "&ТолькоПроведенные" not in query.text
    assert "СтрокиТоваров.Серия" not in query.text
    assert "СтрокиТоваров.Упаковка" not in query.text


def test_query_text_is_loaded_from_file():
    query = load_query_catalog()[0]
    query_path = Path(__file__).parents[1] / "queries_1c" / query.filename

    assert query.text == query_path.read_text(encoding="utf-8")


def test_catalog_is_read_only_and_only_exposes_query_text():
    catalog = load_query_catalog()

    assert isinstance(catalog, tuple)
    assert not hasattr(catalog[0], "execute")
    with pytest.raises(FrozenInstanceError):
        catalog[0].title = "Изменённое название"


def test_query_file_has_no_template_placeholders():
    query = load_query_catalog()[0]

    assert "TODO" not in query.text
    assert "..." not in query.text
    assert query.text.endswith("\n")
