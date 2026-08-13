# Architecture after PR #189

## Concrete boundaries

| Change area | Module to edit |
|---|---|
| entrypoint/page composition | `app.py`, `virtual_warehouse_app.py` |
| revision-aware geometry composition | `warehouse_geometry_render_service.py` |
| static/dynamic rendering algorithms | `warehouse_geometry_render_layers.py` |
| pure map labels/manual-cell validation | `warehouse_map_helpers.py` |
| render-settings persistence | `warehouse_render_settings.py` |
| atomic JSON, signatures, generic JSONL | `warehouse_persistence.py` |
| factual gzip artifact I/O/publication | `warehouse_factual_artifacts.py` |
| factual contracts/import/registry/readiness façade | `warehouse_factual_data.py` |
| revisions and Streamlit read-through cache | `warehouse_revisions.py`, `warehouse_state_cache.py` |
| graph/routing and optimizer | `warehouse_physical_graph.py`, `warehouse_proposed_placement_optimizer.py` |
| performance | existing `warehouse_performance*`, `warehouse_perf_diagnostics.py` and benchmark scripts |

The benchmark and application both call `warehouse_geometry_render_service.render_geometry_layers`; there is no benchmark-only orchestration. `virtual_warehouse_app.py` is reduced from the audited 3,101 to 3,020 LOC. Two responsibility clusters left it: pure map validation/labels and the measured static/dynamic revision/render orchestration. Render settings and generic persistence had already moved in the first PR stage. The outbound experiment screen is a single explicit lazy boundary, keeping scenario/optimizer dependencies out of ordinary startup. Compatibility function names used by existing tests/callers remain.

`warehouse_factual_data.py` remains the public compatibility façade and is reduced from 1,440 to 1,428 LOC. Gzip JSONL iteration/materialization, artifact writing and atomic publication now live in `warehouse_factual_artifacts.py`; source detection, contracts, business keys, normalization and readiness semantics did not move or change.

## Persistence and error rules

Atomic JSON uses a unique adjacent temporary, flush/fsync, closes it, then `os.replace` (Windows-safe). Failed serialization/publication preserves the old artifact. Missing JSON may use an explicit default; malformed JSON is not silently treated as empty. Factual JSONL is lazy and gzip-backed. Domain modules still own schema validation.

## Cache and revision rules

Static geometry depends on geometry + render-settings revisions. Dynamic geometry also depends on placements and outbound. The render service reads revision state once and derives both tokens from that same in-memory state; the state cache performs its separate revision read for its own key. State cache keys add model identity and `(exists, mtime_ns,size)`. Mutations save before bumping affected domains; signature changes catch external edits. Corrupt revision metadata bypasses cached builders. Existing contract tests cover selective placement/inventory, receipts, outbound, geometry/render settings, model isolation, signature changes, and unchanged-cache hits.

## Compatibility

There are no persisted schema migrations. `factual-july-v5`, SKU identity, `РасчетноеОтгруженоКоробок`, exact historical-cell resolution, readiness/VGH semantics, CURRENT/FACT, PROPOSED constraints, routing distances and monthly-fact-v2 remain unchanged. `queries_1c` was not touched.
