# PR #163 monthly data readiness audit

## Historical cell identity audit

| Historical source value | Possible model evidence | Existing parser/resolver | Exact mapping proven? | Risk / decision |
|---|---|---|---|---|
| `Ячейка` free-form string | geometry `cell_key` (`row_number|cell_number|tier`) | `warehouse_inventory_placement._parse_address` | No | The parser supplies defaults and is not evidence that the historical export uses the same address grammar. It is not reused. |
| `Ячейка` free-form string | actual-inventory `АдресЯчейки` | `warehouse_actual_inventory_import._address` | No | This is a different source contract and does not prove historical identity. It is not reused. |
| `Ячейка` free-form string | geometry display/code fields | render/display helpers | No | Visual similarity, punctuation removal, numeric coercion, and a default tier are prohibited. |
| `Ячейка` free-form string | explicit geometry `source_cell` | exact trimmed/NBSP-normalized comparison | Yes, when the model explicitly supplies that authority field and exactly one cell matches | Zero matches remain unresolved; multiple matches are ambiguous. |
| user-selected historical value | exact geometry `cell_key` in the current model signature | persisted factual mapping registry | Yes, after explicit Save | Mapping is scoped to the model ID plus sorted cell-key signature; a changed model cannot silently reuse it. |

The physical graph already exposes mapped cell links and access node IDs. The
readiness API therefore accepts the set of usable graph cell keys rather than
creating graph nodes or inferring access from SVG coordinates.

## Effective evidence architecture

Document sources use the confirmed keys `document_ref + line_number`
(outbound/receipts) and `inventory_ref + line_number` (inventory). VGH uses
`sku_key`. During XLSX import a compact gzip business index records the key,
material-payload fingerprint, minimal material preview, day, dataset, filename,
and source row. Effective views collapse one-payload evidence deterministically
while returning every occurrence. More than one payload fingerprint is an
authoritative conflict and strict access refuses the view. Placement has no
invented row identity: multiple active logical datasets for one snapshot day
block the view.

## Streaming and remaining memory bounds

Production `.xlsx` import uses openpyxl read-only iteration and incremental gzip
RAW, canonical partition, and business-index writers. Publication is a staging
directory rename followed by an atomic registry update. A failure removes the
staging directory before any lifecycle mutation. The full-table pandas path is
retained only for legacy `.xls`, explicit reimport compatibility, and existing
small helper tests.

The importer retains the uploaded XLSX bytes, openpyxl's read-only worksheet
window, exact SHA-256 fingerprints for unique RAW rows, canonical SKU keys,
business-key evidence, and per-day aggregate sets. Those indexes remain
`O(unique rows/keys)` where exact duplicate and conflict detection requires it,
but are materially smaller than RAW/canonical dictionaries. No complete RAW,
canonical, or per-partition row list is retained.

## Replay boundary

`load_effective_rows`, `load_effective_placement`,
`resolve_historical_cell`, `build_fact_route_readiness`, and
`build_monthly_data_readiness` form the single PR #164 input boundary. They do
not apply receipts, rebuild PROPOSED, simulate stock, calculate routes, or
calculate monthly savings.
