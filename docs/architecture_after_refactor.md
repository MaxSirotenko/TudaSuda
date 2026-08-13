# Architecture after the stability refactor

## Where to make a change

| Concern | Primary module/boundary |
|---|---|
| entrypoint/page composition | `app.py`, `virtual_warehouse_app.py` |
| operational screens | `warehouse_workspace_ui.py`, other `warehouse_*_ui.py` |
| geometry model | `warehouse_geometry_model.py` |
| static/dynamic rendering | `warehouse_geometry_render_layers.py`; app owns Streamlit decorators only |
| render settings persistence | `warehouse_render_settings.py` |
| factual contracts/import/registry | compatibility surface `warehouse_factual_data.py` |
| receipts/outbound/inventory | corresponding `warehouse_receipts.py`, `warehouse_outbound_orders.py`, `warehouse_inventory_placement.py` |
| event/state simulation | `warehouse_event_*`, `warehouse_simulation_*` |
| monthly FACT/checkpoints | `warehouse_monthly_fact_replay.py` |
| placement optimization | `warehouse_proposed_placement_optimizer.py` |
| physical routing | `warehouse_physical_graph.py` and replay adapters |
| atomic JSON/file signatures/JSONL | `warehouse_persistence.py` |
| revision counters | `warehouse_revisions.py` |
| Streamlit state read-through cache | `warehouse_state_cache.py` |
| timing/diagnostics/benchmarks | existing `warehouse_performance.py`, `warehouse_perf_diagnostics.py`, `warehouse_performance_benchmark.py` |

## Boundaries

UI owns buttons, progress, messages and session state. Domain functions receive inputs and return results. `warehouse_state_cache` is explicitly a UI infrastructure adapter: core loaders remain Streamlit-free. Persistence primitives provide mechanics only; validation and schema compatibility remain in domain modules.

Writes use an adjacent uniquely named temporary, flush/fsync, close it, then `os.replace`; this preserves the previous valid artifact on serialization/publication failure and remains Windows-compatible. Missing JSON may have an explicit default, while malformed JSON is not silently converted to empty data. JSONL iteration is lazy and optionally gzip-backed.

## Cache/revision rules

Static geometry depends on geometry + render-settings revisions. Dynamic map state also depends on placements and outbound. Persisted state caches add model identity and `(exists, mtime_ns, size)` signature. Successful mutations save before bumping affected revisions. External edits are detected by signatures; damaged revision metadata forces direct reads. Caches are bounded where state fan-out can grow. Persisted factual/monthly artifacts are not copied into session state.

## Compatibility

No artifact schema, parser version, Data Contract, SKU identity, routing policy, or business constraint changed. Existing `data/last_import` JSON, factual v5, and monthly-fact-v2 artifacts remain readable. Public app helper names remain compatibility wrappers while persistence implementation moved behind focused modules.
