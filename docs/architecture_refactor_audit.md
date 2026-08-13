# Architecture refactor audit (base `819fa524`)

## Scope and method

This audit was taken before production changes on the supplied PR #188 snapshot. The managed environment has no Git remote; the available merge history nevertheless contains #164, #180, #183, #185, #187 and #188. Evidence came from AST import inspection, LOC/byte counts, searches for Streamlit, JSON/file APIs, cache decorators and catch-all handlers, plus the existing deterministic 16,000-cell performance benchmark. Flags in the inventory are conservative indicators, not proof that every use is on a hot path. “globals” includes module constants as well as mutable objects and therefore requires human review.

## Findings

* `virtual_warehouse_app.py` (3,101 LOC) is the principal composition root but also owns render cache keys, persisted render settings, model mutations and many pure map helpers. Its eager imports make startup transitively load pandas, Streamlit and most operational subsystems.
* The largest domain monoliths are factual data (1,440 LOC/91,808 bytes), geometry (1,290/66,990), inventory placement (1,262/69,833), and optimizer (1,102/70,890). Their public surfaces are extensively used, so a compatibility façade is safer than a mass package migration.
* Streamlit is limited mostly to entry/UI adapters. `warehouse_state_cache.py` is a deliberate adapter but combines Streamlit caching with file-signature policy; signature calculation can be dependency-free.
* Atomic JSON implementations were duplicated in revisions, factual ingestion, monthly replay and the app. Implementations differ in fsync, temporary naming, cleanup and Windows safety. Render settings used a fixed `.tmp`, allowing collisions.
* Revision-aware state and static/dynamic render caches already prevent the most expensive no-op work. Keys are domain revision token + model identity and, for persisted state, file signature. Corrupt revision state correctly bypasses rather than trusts cache.
* Catch-all exceptions are common at UI/recovery boundaries. Progress callbacks intentionally cannot abort factual ingestion. Programming errors should otherwise propagate; this refactor does not mechanically alter catches without contract evidence.

## Logical dependency map

```text
app.py
  -> virtual_warehouse_app / warehouse_*_ui       (presentation/composition)
      -> workspace/scenario orchestration
          -> geometry, factual, placement, simulation, optimizer (domain)
              -> artifact/state modules
                  -> warehouse_persistence, revisions, performance (utilities)
```

Risks are not hard Python import cycles today, but wide façade imports: performance benchmark imports the app to exercise production caches, while the app imports performance instrumentation. Domain-to-UI reverse dependencies were not found in core factual/geometry/simulation/optimizer modules. Duplicate persistence and normalization are the higher-confidence coupling problems. Heavy startup boundaries are pandas/openpyxl and the broad app import; lazy imports should be introduced only at screen/operation boundaries because scattered local imports would obscure contracts.

## Cache and artifact matrix

| Object | Key/invalidation | Storage | Signature |
|---|---|---|---|
| placement state | model + placements/inventory revisions + file signature | Streamlit memory; JSON source | yes |
| receipts | model + receipts revision + file signature | Streamlit memory; JSON source | yes |
| outbound states | model + outbound revision + each file signature | Streamlit memory; JSON source | yes |
| static geometry layer | geometry/render-settings revisions + renderer version/options | Streamlit memory | revision-backed |
| dynamic map payload | geometry/placements/outbound/render-settings revisions + renderer version | Streamlit memory | revision-backed |
| factual v5 partitions/index | parser/source contract + registry metadata | persisted JSONL/gzip/SQLite artifacts | artifact metadata |
| monthly FACT v2 | scenario/input identity + checkpoint manifest | persisted, daily lazy details | manifest/checkpoint |

Mutation APIs must save first and bump every affected revision second. A signature is a safety net for external edits; it is not a substitute for a revision bump. Corrupt/missing revision metadata bypasses memory cache. No persisted schema or parser version is changed here.

## Hot paths and baseline

| Path | Work and likely bottleneck | Baseline / evidence |
|---|---|---|
| startup | eager transitive imports | reproducible `application_import` scenario |
| warm no-op rerun | revision reads, cache hits, final HTML composition | builders 0; benchmark now reports elapsed, reads, peak and payload |
| map render | static SVG/DOM generation, sparse dynamic projection, composition | 16k: static cold 6429.844 ms; static warm 158.821 ms; dynamic cold 226.346 ms; warm 1.969 ms; compose 217.577 ms; 22.02 MiB HTML |
| factual XLSX | openpyxl read-only streaming and staged publication | retain #187/#188 benchmark script; never pandas materialization |
| readiness | streaming business indexes + SQLite conflicts | retain #180 bounded-memory design |
| one-day FACT | effective daily sources and deterministic replay | existing factual/monthly tests |
| monthly resume/details | manifest/checkpoint read; lazy day detail | retain #183 day-by-day checkpoints |
| PROPOSED/optimizer | eligibility filtering, candidate sorting/distance | optimizer tests/benchmark candidate for follow-up |
| routing | immutable graph/index and repeated shortest paths | physical graph and deterministic route tests |

The before run used `python scripts/run_warehouse_performance_benchmark.py --mode synthetic --cells 16000 --occupied-cells 500 --placements 700 --warm-iterations 3 --import-iterations 3 --output-dir /tmp/tudasuda-before`. Wall-clock data is diagnostic, not a test SLA.

## Refactor decisions and non-goals

This change introduces one small persistence utility and a render-settings boundary, keeps compatibility wrappers, and extends the existing benchmark rather than creating another framework. It does not change factual parser v5, SKU/outbound/readiness/VGH/FACT/PROPOSED/routing semantics, persisted schemas, queries, or startup scripts. Splitting factual parsing and the 3,101-line UI façade further remains valuable but is deliberately not done by mechanical movement without profiling and caller migration evidence.

## Production module inventory

| Module | LOC | Main internal imports | Imported by | st | I/O | globals | cache | domain | UI |
|---|---:|---|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `app.py` | 10 | virtual_warehouse_app | entry/tests | — | — | — | — | mixed | Y |
| `row_constructor.py` | 1647 | — | entry/tests | Y | — | Y | — | Y | Y |
| `virtual_warehouse_app.py` | 3101 | warehouse_outbound_experiment_ui, warehouse_performance, warehouse_revisions, warehouse_geometry_model, warehouse_geometry_render_layers | app, browser_map_benchmark_app, warehouse_performance_benchmark | Y | Y | Y | Y | mixed | Y |
| `warehouse_actual_inventory_import.py` | 469 | warehouse_inventory_placement, warehouse_business_identity | warehouse_outbound_experiment_inputs, warehouse_outbound_experiment_ui, warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_addressing.py` | 62 | — | entry/tests | — | — | Y | — | Y | — |
| `warehouse_browser_benchmark.py` | 178 | — | browser_map_benchmark_app | — | Y | Y | — | Y | — |
| `warehouse_business_identity.py` | 74 | — | warehouse_actual_inventory_import, warehouse_day_receipt_scenario_inputs, warehouse_day_receipts_import, warehouse_event_reducer | — | — | Y | — | Y | — |
| `warehouse_cross_aisles.py` | 225 | — | virtual_warehouse_app, warehouse_row_settings | — | — | — | — | Y | — |
| `warehouse_day_benchmark.py` | 115 | warehouse_proposed_scenario, warehouse_simulation_distance_comparison, warehouse_simulation_outbound_replay, warehouse_simulation_state | warehouse_scenario_comparison_ui | — | — | Y | — | Y | — |
| `warehouse_day_receipt_scenario_inputs.py` | 299 | warehouse_business_identity | warehouse_outbound_experiment_ui | — | Y | Y | — | Y | — |
| `warehouse_day_receipts_import.py` | 280 | warehouse_business_identity | warehouse_outbound_experiment_ui | — | — | Y | — | Y | — |
| `warehouse_deep_lane.py` | 77 | — | warehouse_geometry_model, warehouse_row_settings, warehouse_simulation_state | — | Y | Y | — | Y | — |
| `warehouse_event_reducer.py` | 470 | warehouse_business_identity, warehouse_simulation_state, warehouse_palletization | entry/tests | — | Y | Y | — | Y | — |
| `warehouse_event_timeline.py` | 375 | — | entry/tests | — | Y | Y | — | Y | — |
| `warehouse_factual_data.py` | 1440 | warehouse_perf_diagnostics, warehouse_business_identity | warehouse_monthly_fact_replay, warehouse_monthly_placement_comparison, warehouse_workspace_ui | — | Y | Y | — | Y | — |
| `warehouse_geometry_model.py` | 1290 | warehouse_deep_lane, warehouse_placement_zones | virtual_warehouse_app, warehouse_geometry_render_layers, warehouse_performance_benchmark | — | Y | Y | — | Y | — |
| `warehouse_geometry_render_layers.py` | 203 | warehouse_geometry_model, warehouse_performance, warehouse_placement_diagnostics | browser_map_benchmark_app, virtual_warehouse_app, warehouse_performance_benchmark, warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_inventory_placement.py` | 1262 | warehouse_placement_diagnostics, warehouse_business_identity | virtual_warehouse_app, warehouse_actual_inventory_import, warehouse_outbound_orders, warehouse_outbound_scenario_replay | — | Y | Y | — | Y | — |
| `warehouse_inventory_results_import.py` | 266 | warehouse_business_identity | warehouse_outbound_experiment_ui | — | — | Y | — | Y | — |
| `warehouse_inventory_target_scope.py` | 202 | warehouse_outbound_orders | entry/tests | — | — | Y | — | Y | — |
| `warehouse_monthly_fact_replay.py` | 281 | warehouse_factual_data, warehouse_physical_graph, warehouse_simulation_outbound_replay | warehouse_workspace_ui | — | Y | Y | — | Y | — |
| `warehouse_monthly_placement_comparison.py` | 272 | warehouse_factual_data | warehouse_workspace_ui | — | Y | Y | — | Y | — |
| `warehouse_opening_stock_reconciliation.py` | 273 | warehouse_business_identity | warehouse_outbound_experiment_inputs, warehouse_scenario_comparison_ui | — | — | — | — | Y | — |
| `warehouse_outbound_experiment_inputs.py` | 282 | warehouse_opening_stock_reconciliation, warehouse_actual_inventory_import, warehouse_pick_demands, warehouse_business_identity, warehouse_placement_zones | warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_outbound_experiment_pipeline.py` | 241 | warehouse_outbound_scenario_comparison, warehouse_outbound_scenario_replay, warehouse_physical_graph, warehouse_receipt_current_placements, warehouse_receipt_proposed_placements | entry/tests | — | Y | Y | — | Y | — |
| `warehouse_outbound_experiment_ui.py` | 372 | warehouse_actual_inventory_import, warehouse_day_receipt_scenario_inputs, warehouse_day_receipts_import, warehouse_inventory_results_import, warehouse_outbound_orders | virtual_warehouse_app | Y | Y | Y | Y | mixed | Y |
| `warehouse_outbound_orders.py` | 544 | warehouse_business_identity, warehouse_inventory_placement | virtual_warehouse_app, warehouse_inventory_target_scope, warehouse_outbound_experiment_ui, warehouse_pick_demands | — | Y | Y | — | Y | — |
| `warehouse_outbound_scenario_comparison.py` | 264 | — | warehouse_outbound_experiment_pipeline | — | Y | Y | — | Y | — |
| `warehouse_outbound_scenario_replay.py` | 378 | warehouse_placement_zones, warehouse_inventory_placement, warehouse_physical_graph, warehouse_pick_inventory, warehouse_pick_working_stock | warehouse_outbound_experiment_pipeline | — | Y | Y | Y | Y | — |
| `warehouse_palletization.py` | 177 | warehouse_business_identity | warehouse_event_reducer | — | Y | — | — | Y | — |
| `warehouse_perf_diagnostics.py` | 98 | — | warehouse_factual_data, warehouse_workspace_ui | — | Y | Y | Y | Y | — |
| `warehouse_performance.py` | 180 | — | virtual_warehouse_app, warehouse_geometry_render_layers | — | Y | Y | — | Y | — |
| `warehouse_performance_benchmark.py` | 530 | virtual_warehouse_app, warehouse_geometry_model, warehouse_inventory_placement, warehouse_revisions, warehouse_state_cache | browser_map_benchmark_app | Y | Y | Y | Y | Y | Y |
| `warehouse_persistence.py` | 88 | — | virtual_warehouse_app, warehouse_render_settings, warehouse_revisions, warehouse_state_cache | — | Y | — | Y | Y | — |
| `warehouse_physical_graph.py` | 310 | — | warehouse_monthly_fact_replay, warehouse_outbound_experiment_pipeline, warehouse_outbound_scenario_replay, warehouse_proposed_placement_optimizer | — | Y | Y | — | Y | — |
| `warehouse_pick_candidates.py` | 203 | — | entry/tests | — | — | Y | — | Y | — |
| `warehouse_pick_demands.py` | 270 | warehouse_business_identity, warehouse_outbound_orders | warehouse_outbound_experiment_inputs, warehouse_scenario_comparison_ui | — | Y | — | — | Y | — |
| `warehouse_pick_inventory.py` | 277 | warehouse_inventory_placement, warehouse_inventory_placement | warehouse_outbound_scenario_replay | — | — | Y | — | Y | — |
| `warehouse_pick_working_stock.py` | 200 | — | warehouse_outbound_scenario_replay | — | Y | Y | — | Y | — |
| `warehouse_placement_diagnostics.py` | 508 | — | virtual_warehouse_app, warehouse_geometry_render_layers, warehouse_inventory_placement | — | Y | Y | — | Y | — |
| `warehouse_placement_rules.py` | 196 | — | warehouse_proposed_placement_optimizer, warehouse_proposed_scenario, warehouse_simulation_outbound_replay | — | Y | Y | — | Y | — |
| `warehouse_placement_zones.py` | 80 | — | warehouse_geometry_model, warehouse_outbound_experiment_inputs, warehouse_outbound_experiment_ui, warehouse_outbound_scenario_replay | — | — | Y | — | Y | — |
| `warehouse_proposed_placement_optimizer.py` | 1102 | warehouse_placement_rules, warehouse_placement_zones, warehouse_physical_graph, warehouse_sku_adjacency | warehouse_proposed_scenario, warehouse_proposed_state | — | Y | Y | — | Y | — |
| `warehouse_proposed_scenario.py` | 110 | warehouse_placement_rules, warehouse_proposed_placement_optimizer, warehouse_proposed_state, warehouse_sku_adjacency | warehouse_day_benchmark, warehouse_scenario_comparison_ui | — | Y | — | — | Y | — |
| `warehouse_proposed_state.py` | 206 | warehouse_proposed_placement_optimizer, warehouse_simulation_state | warehouse_proposed_scenario | — | — | Y | — | Y | — |
| `warehouse_receipt_current_placements.py` | 236 | — | warehouse_outbound_experiment_pipeline | — | Y | Y | — | Y | — |
| `warehouse_receipt_proposed_placements.py` | 204 | warehouse_placement_zones | warehouse_outbound_experiment_pipeline | — | Y | Y | — | Y | — |
| `warehouse_receipt_snapshot_transitions.py` | 239 | warehouse_inventory_placement | warehouse_outbound_experiment_pipeline | — | Y | Y | — | Y | — |
| `warehouse_receipt_virtual_slots.py` | 279 | — | warehouse_outbound_experiment_pipeline | — | Y | Y | — | Y | — |
| `warehouse_receipts.py` | 586 | warehouse_business_identity, warehouse_placement_zones | virtual_warehouse_app, warehouse_state_cache | — | Y | Y | — | Y | — |
| `warehouse_render_settings.py` | 41 | warehouse_persistence | virtual_warehouse_app | — | — | Y | — | Y | — |
| `warehouse_revisions.py` | 155 | warehouse_persistence | virtual_warehouse_app, warehouse_performance_benchmark, warehouse_state_cache | — | — | Y | — | Y | — |
| `warehouse_route_ui.py` | 83 | — | virtual_warehouse_app, warehouse_scenario_comparison_ui, warehouse_workspace_ui | Y | — | Y | — | mixed | Y |
| `warehouse_row_settings.py` | 677 | warehouse_placement_zones, warehouse_deep_lane, warehouse_cross_aisles, warehouse_cross_aisles | virtual_warehouse_app | — | — | Y | — | Y | — |
| `warehouse_scenario_comparison_ui.py` | 513 | warehouse_business_identity, warehouse_geometry_render_layers, warehouse_opening_stock_reconciliation, warehouse_outbound_experiment_inputs, warehouse_actual_inventory_import | warehouse_outbound_experiment_ui, warehouse_workspace_ui | Y | Y | Y | Y | mixed | Y |
| `warehouse_simulation_distance_comparison.py` | 169 | — | warehouse_day_benchmark | — | Y | Y | — | Y | — |
| `warehouse_simulation_outbound_replay.py` | 291 | warehouse_physical_graph, warehouse_placement_rules, warehouse_placement_zones, warehouse_simulation_state | warehouse_day_benchmark, warehouse_monthly_fact_replay | — | Y | Y | Y | Y | — |
| `warehouse_simulation_render.py` | 98 | warehouse_geometry_render_layers | warehouse_scenario_comparison_ui | — | — | — | — | Y | — |
| `warehouse_simulation_state.py` | 601 | warehouse_business_identity, warehouse_deep_lane | warehouse_day_benchmark, warehouse_event_reducer, warehouse_proposed_state, warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_sku_adjacency.py` | 65 | warehouse_business_identity | warehouse_proposed_placement_optimizer, warehouse_proposed_scenario, warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_sku_velocity.py` | 106 | warehouse_business_identity | warehouse_scenario_comparison_ui | — | Y | Y | — | Y | — |
| `warehouse_state_cache.py` | 171 | warehouse_inventory_placement, warehouse_outbound_orders, warehouse_receipts, warehouse_revisions, warehouse_persistence | virtual_warehouse_app, warehouse_outbound_experiment_ui, warehouse_performance_benchmark | Y | — | Y | Y | Y | Y |
| `warehouse_ui_messages.py` | 99 | — | warehouse_outbound_experiment_ui, warehouse_workspace_ui | Y | — | Y | — | Y | Y |
| `warehouse_workflow_ui_state.py` | 93 | — | warehouse_workspace_ui | — | — | Y | — | Y | — |
| `warehouse_workspace_ui.py` | 884 | warehouse_placement_zones, warehouse_scenario_comparison_ui, warehouse_ui_messages, warehouse_workflow_ui_state, warehouse_factual_data | virtual_warehouse_app | Y | Y | Y | Y | mixed | Y |
| `warehouse_zone_boundaries.py` | 292 | warehouse_placement_zones | virtual_warehouse_app | — | — | Y | — | Y | — |
