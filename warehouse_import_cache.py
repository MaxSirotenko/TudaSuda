"""Streamlit cache adapter for interactive Excel previews and normalization."""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import streamlit as st

from warehouse_geometry_model import normalize_cell_table, read_cell_table
from warehouse_inventory_placement import normalize_inventory_table, read_inventory_table
from warehouse_receipts import normalize_receipt_table, read_receipt_table


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@st.cache_data(show_spinner=False)
def read_cell_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_cell_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_cell_table_cached(table_payload: str, mapping_payload: str):
    return normalize_cell_table(pd.read_json(table_payload, orient="split"), json.loads(mapping_payload))


@st.cache_data(show_spinner=False)
def read_inventory_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_inventory_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_inventory_table_cached(table_payload: str, mapping_payload: str):
    return normalize_inventory_table(pd.read_json(table_payload, orient="split"), json.loads(mapping_payload))


@st.cache_data(show_spinner=False)
def read_receipt_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_receipt_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_receipt_table_cached(table_payload: str, mapping_payload: str):
    return normalize_receipt_table(pd.read_json(table_payload, orient="split"), json.loads(mapping_payload))
