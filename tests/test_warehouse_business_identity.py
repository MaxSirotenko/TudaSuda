import json
import math

import pytest

from warehouse_business_identity import (
    build_canonical_sku_identity,
    canonical_sku_key,
    find_canonical_identity_collisions,
    normalize_business_text,
    normalize_unit_name,
    normalize_warehouse,
    physical_warehouse_key,
    same_physical_warehouse,
    validate_box_quantity,
)
from warehouse_inventory_placement import make_sku_key
from warehouse_outbound_orders import make_outbound_sku_key


def metadata(**changes):
    value = {"nomenclature_code": "123", "nomenclature": "Молоко",
             "characteristic_code": "456", "characteristic": "1 л"}
    value.update(changes)
    return value


def test_cross_source_wrappers_have_one_name_based_v2_identity():
    expected = canonical_sku_key(metadata())
    assert expected.startswith("sku:v2:")
    assert make_sku_key({"sku_code": "123", "sku_name": "Молоко",
                         "characteristic_code": "456", "characteristic_name": "1 л"}) == expected
    assert make_outbound_sku_key("Молоко", "1 л") == expected


@pytest.mark.parametrize("name", [" МОЛОКО ", "молоко", "Молоко"])
@pytest.mark.parametrize("characteristic", [" 1 Л ", "1 л"])
def test_name_permutations_are_stable(name, characteristic):
    assert canonical_sku_key(metadata(nomenclature=name, characteristic=characteristic)) == canonical_sku_key(metadata())


def test_text_characteristic_and_empty_contracts():
    assert {normalize_business_text(x) for x in ("Ёлка", "елка", "ЕЛКА")} == {"елка"}
    assert canonical_sku_key(metadata(characteristic="1 л")) != canonical_sku_key(metadata(characteristic="0.5 л"))
    assert len({canonical_sku_key(metadata(characteristic=x, characteristic_code="")) for x in (None, "", " ")}) == 1


def test_legacy_key_is_not_authoritative_and_mismatch_is_visible():
    result = build_canonical_sku_identity(metadata(sku_key="name:Молоко|char_name:1 л"))
    assert result["sku_key"].startswith("sku:v2:")
    assert result["diagnostics"] == ["legacy_sku_key_mismatch"]


def test_collision_is_not_hidden():
    collisions = find_canonical_identity_collisions([
        metadata(nomenclature_code="123"), metadata(nomenclature_code="999")])
    assert collisions[0]["reason"] == "canonical_identity_collision"
    assert collisions[0]["nomenclature_codes"] == ["123", "999"]


def test_missing_name_fails_closed_even_when_code_exists():
    result = build_canonical_sku_identity(metadata(nomenclature="", sku_name=""))
    assert result["sku_key"] == ""
    assert result["diagnostics"] == ["sku_identity_missing"]


def test_warehouse_normalization_is_exact():
    assert normalize_warehouse(" ДОЛГОСРОК ВЁШКИ ") == normalize_warehouse("долгосрок вешки")
    assert normalize_warehouse("Вешки") != normalize_warehouse("Долгосрок Вешки")


def test_confirmed_veshki_frov_physical_equivalence_keeps_source_scopes_exact():
    receipt_scope = normalize_warehouse("Овощи Фрукты")
    outbound_scope = normalize_warehouse("Комплектация Овощи Фрукты")
    assert receipt_scope != outbound_scope
    assert physical_warehouse_key(receipt_scope) == physical_warehouse_key(outbound_scope)
    assert same_physical_warehouse(receipt_scope, outbound_scope) is True
    assert same_physical_warehouse(receipt_scope, "Другой склад") is False


@pytest.mark.parametrize("value", ["короб", "короба", "коробов", " КОРОБОВ "])
def test_box_unit_aliases(value):
    assert normalize_unit_name(value) == "короб"


def test_box_quantity_contract():
    assert validate_box_quantity(10) == (10, None)
    assert validate_box_quantity(0) == (0, None)
    assert validate_box_quantity(0, positive=True)[0] is None
    for value in (True, 10.5, "10", math.nan, math.inf):
        assert validate_box_quantity(value)[0] is None


def test_deterministic_json_serializable_and_no_mutation():
    source = metadata()
    before = dict(source)
    first = build_canonical_sku_identity(source)
    assert first == build_canonical_sku_identity(dict(reversed(list(source.items()))))
    assert source == before
    json.dumps(first, ensure_ascii=False)
