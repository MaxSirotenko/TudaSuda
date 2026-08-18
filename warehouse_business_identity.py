"""Pure canonical SKU, warehouse and operational box contracts."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

IDENTITY_VERSION = 2
CANONICAL_BOX_UNIT = "короб"


def normalize_business_text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).casefold().replace("ё", "е")


def normalize_warehouse(value: Any) -> str:
    """Normalize for exact equality without typo correction or fuzzy matching."""
    return normalize_business_text(value)


# Source scopes remain exact and separate.  Physical equivalence is a different
# contract used only when deciding whether one mutable physical placement may be
# consumed by an operational source scope.  Add aliases only after the business
# relation has been explicitly confirmed.
PHYSICAL_WAREHOUSE_GROUPS = {
    "veshki_frov": frozenset({
        normalize_warehouse("Овощи Фрукты"),
        normalize_warehouse("Комплектация Овощи Фрукты"),
    }),
}
_PHYSICAL_WAREHOUSE_BY_SOURCE = {
    source_scope: physical_key
    for physical_key, source_scopes in PHYSICAL_WAREHOUSE_GROUPS.items()
    for source_scope in source_scopes
}


def physical_warehouse_key(value: Any) -> str:
    """Resolve a confirmed physical warehouse without changing source scope identity."""
    source_scope = normalize_warehouse(value)
    return _PHYSICAL_WAREHOUSE_BY_SOURCE.get(source_scope, source_scope)


def same_physical_warehouse(left: Any, right: Any) -> bool:
    """Compare confirmed physical scope while keeping factual source filtering exact."""
    left_key, right_key = physical_warehouse_key(left), physical_warehouse_key(right)
    return bool(left_key and right_key and left_key == right_key)


def normalize_unit_name(value: Any) -> str | None:
    value = normalize_business_text(value)
    return CANONICAL_BOX_UNIT if value in {"короб", "короба", "коробов"} else None


def build_canonical_sku_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Build name-based v2 identity; codes are retained as collision evidence."""
    name = normalize_business_text(metadata.get("nomenclature") or metadata.get("sku_name") or metadata.get("item_name"))
    characteristic = normalize_business_text(metadata.get("characteristic") or metadata.get("characteristic_name"))
    diagnostics: list[str] = []
    key = ""
    if not name:
        diagnostics.append("sku_identity_missing")
    else:
        key = f"sku:v2:name={quote(name, safe='')}|characteristic={quote(characteristic, safe='')}"
    imported = str(metadata.get("sku_key") or "").strip()
    if imported and key and imported != key:
        diagnostics.append("legacy_sku_key_mismatch")
    return {"identity_version": IDENTITY_VERSION, "sku_key": key, "nomenclature": name,
            "characteristic": characteristic,
            "nomenclature_code": normalize_business_text(metadata.get("nomenclature_code") or metadata.get("sku_code")),
            "characteristic_code": normalize_business_text(metadata.get("characteristic_code")),
            "diagnostics": diagnostics}


def canonical_sku_key(metadata: Mapping[str, Any]) -> str:
    return build_canonical_sku_identity(metadata)["sku_key"]


def find_canonical_identity_collisions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    evidence: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        identity = build_canonical_sku_identity(row)
        if not identity["sku_key"]:
            continue
        values = evidence.setdefault(identity["sku_key"], {"nomenclature_codes": set(), "characteristic_codes": set()})
        for source, target in (("nomenclature_code", "nomenclature_codes"), ("characteristic_code", "characteristic_codes")):
            if identity[source]:
                values[target].add(identity[source])
    return [{"reason": "canonical_identity_collision", "sku_key": key,
             "nomenclature_codes": sorted(values["nomenclature_codes"]),
             "characteristic_codes": sorted(values["characteristic_codes"])}
            for key, values in sorted(evidence.items())
            if len(values["nomenclature_codes"]) > 1 or len(values["characteristic_codes"]) > 1]


def validate_box_quantity(value: Any, *, positive: bool = False) -> tuple[int | None, str | None]:
    """Validate parsed quantities; accepting numeric strings belongs to import boundaries."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, "invalid_box_quantity"
    if value < 0 or not float(value).is_integer() or (positive and value == 0):
        return None, "invalid_box_quantity"
    return int(value), None
