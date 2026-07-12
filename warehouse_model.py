from dataclasses import dataclass, field
from typing import Any

@dataclass
class WarehouseCell:
    sheet_name: str
    row_number: str
    cell_number: str
    tier_number: int
    address: str
    x: int
    y: int
    width: int = 1
    height: int = 1
    source: str = "auto"
    item: str = ""

    fill_color: str = ""
    value: str = ""

    warnings: list[str] = field(default_factory=list)

@dataclass
class WarehouseRow:
    sheet_name: str
    row_number: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    direction: str
    confidence: float
    potential_cells: list[WarehouseCell] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

@dataclass
class WarehouseSheet:
    name: str
    max_row: int
    max_column: int
    values: list[dict[str, Any]]
    merged_ranges: list[str]
    rows: list[WarehouseRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

@dataclass
class WarehouseModel:
    sheets: list[WarehouseSheet]
    diagnostics: list[dict[str, str]] = field(default_factory=list)

    @property
    def cells(self) -> list[WarehouseCell]:
        return [cell for sheet in self.sheets for row in sheet.rows for cell in row.potential_cells]

    def cell_index(self) -> dict[str, WarehouseCell]:
        return {cell.address: cell for cell in self.cells}
