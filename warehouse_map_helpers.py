"""Pure address and validation helpers used by the warehouse map editor."""

from __future__ import annotations

from typing import Any, Callable


def is_numeric_text(value: str) -> bool:
    try:
        float(str(value).strip())
        return str(value).strip() != ""
    except ValueError:
        return False


def cell_label(cell: dict[str, Any]) -> str:
    code = cell.get("code") or "без кода"
    return f"ряд {cell.get('row_number')} · ячейка {cell.get('cell_number')} · ярус {cell.get('tier')} · {code}"


def short_cell_value(cell: dict[str, Any] | None) -> str:
    if not cell:
        return "—"
    return f"ряд {cell.get('row_number')}, ячейка {cell.get('cell_number')}, ярус {cell.get('tier')}, код {cell.get('code') or '—'}"


def source_label(value: str | None) -> str:
    return {"excel": "Excel", "manual_add": "добавлена вручную", "manual_update": "изменена вручную"}.get(
        str(value or "excel"), str(value or "Excel")
    )


def validate_manual_cell(
    model: dict[str, Any], new_cell: dict[str, Any], original_key: str | None,
    *, key_builder: Callable[[dict[str, Any]], str],
) -> list[str]:
    errors = []
    for field, empty_message, numeric_message in (
        ("row_number", "Ряд не может быть пустым.", "Ряд должен быть числом."),
        ("cell_number", "Номер ячейки не может быть пустым.", "Номер ячейки должен быть числом."),
        ("tier", "Ярус не может быть пустым.", "Ярус должен быть числом."),
    ):
        value = str(new_cell.get(field, "")).strip()
        if not value:
            errors.append(empty_message)
        elif not is_numeric_text(value):
            errors.append(numeric_message)
    new_key = key_builder(new_cell)
    if any(key_builder(cell) == new_key and new_key != original_key for cell in model.get("cells", [])):
        errors.append("Ячейка с такой комбинацией ряд + номер ячейки + ярус уже существует.")
    return errors
