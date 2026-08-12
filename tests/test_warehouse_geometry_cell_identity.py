from __future__ import annotations

import json

import pandas as pd

import warehouse_geometry_model as geometry
from warehouse_factual_data import resolve_historical_cell


def _geometry_rows(*tiers: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": f"code-{tier}",
                "row_number": "152",
                "cell_number": "10",
                "tier": tier,
                "source": "excel",
                "source_line": index + 2,
            }
            for index, tier in enumerate(tiers)
        ]
    )


def test_geometry_builds_authoritative_cell_identity(tmp_path):
    model, _ = geometry.build_geometry_model(
        _geometry_rows("1"),
        geometry.GeometrySettings(),
        source_file_name="cells.xlsx",
        source_sheet_name="cells",
    )

    cell = model["cells"][0]
    assert cell["cell_key"] == "152|10|1"
    assert cell["source_cell"] == "152-10"

    resolution = resolve_historical_cell("152-10", model, root=tmp_path)
    assert resolution["geometry_cell_key"] == "152|10|1"
    assert resolution["mapping_method"] == "exact_authoritative"


def test_geometry_identity_does_not_guess_between_tiers(tmp_path):
    model, _ = geometry.build_geometry_model(
        _geometry_rows("1", "2"),
        geometry.GeometrySettings(tier_mode="all"),
    )

    resolution = resolve_historical_cell("152-10", model, root=tmp_path)
    assert resolution["resolution_status"] == "ambiguous"
    assert resolution["candidates"] == ["152|10|1", "152|10|2"]


def test_load_legacy_geometry_backfills_cell_identity(tmp_path, monkeypatch):
    model_path = tmp_path / "warehouse_model.json"
    monkeypatch.setattr(geometry, "GEOMETRY_MODEL_PATH", model_path)
    monkeypatch.setattr(geometry, "MANUAL_OVERRIDES_PATH", tmp_path / "manual_overrides.json")

    model_path.write_text(
        json.dumps(
            {
                "model_type": "excel_rows_cells_aisles_geometry",
                "model_id": "legacy-model",
                "cells": [
                    {
                        "code": "169552",
                        "row_number": "152",
                        "cell_number": "10",
                        "tier": "1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = geometry.load_geometry_model()

    assert loaded is not None
    for collection_name in ("cells", "base_cells"):
        cell = loaded[collection_name][0]
        assert cell["cell_key"] == "152|10|1"
        assert cell["source_cell"] == "152-10"
