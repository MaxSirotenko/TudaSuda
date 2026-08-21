# Production source-authority audit

Audit performed from merged PR #190 snapshot. Classification: **A** source of truth, **B** derived state, **C** user configuration, **D** cache, **E** result artifact. The repository-wide static scan is executable as `python scripts/audit_source_authority.py`.

| Screen / operation | Data family | Current reader / writer | Current artifact | Authoritative source | Status | Action |
|---|---|---|---|---|---|---|
| Data upload workspace | all factual | `import_excel_dataset` | factual registry/partitions | factual effective views (A) | OK | none |
| CURRENT/PROPOSED V1 | START | formerly `_excel_upload` / actual-inventory importer | uploaded bytes | factual historical placement (A) | MIGRATED | adapter default; explicit manual radio |
| CURRENT/PROPOSED V1 | outbound | formerly `load_outbound_orders_cached` | `outbound_orders.json` (manual A) | factual outbound (A) | MIGRATED | scoped adapter; legacy only explicit fallback |
| CURRENT/PROPOSED V1 | receipts | day workbook / receipts JSON | manual state | factual receipts (A) | MIGRATED | adapter provides optional day input |
| CURRENT/PROPOSED V1 | inventory | factual evidence / explicit control workbook | factual source quantities or proven manual boxes | factual inventory is diagnostic evidence | CORRECTED | never promote `actual_quantity` to boxes; manual control remains explicit |
| CURRENT/PROPOSED V1 | VGH | receipt classification overlay | derived zones | factual VGH (A) | OK | classification remains derived |
| Monthly FACT / comparison | placement, outbound | effective factual loaders | factual partitions | factual (A) | OK | preserve mathematics |
| Scenario comparison / proposed scenario | scenario inputs | caller-provided immutable states | in-memory/result | factual adapter upstream | OK | engine unchanged |
| Route visualization | routes | replay result | replay graph/legs (E) | factual outbound upstream | OK | no independent source reader |
| Guided workflow | readiness | session scenario demand/baseline | session cache (D) | same factual scenario inputs | ALIGNED | factual adapter populates downstream state |
| Receipt zoning screen | VGH | `load_effective_rows("vgh")` | factual view | factual VGH (A) | OK | none |
| Receipt zoning / placement workspace | receipts | factual scenario adapter by default | effective factual receipts | factual receipts (A) | MIGRATED | `receipts.json`/upload available only after explicit manual fallback selection |
| Outbound picking workspace | outbound | factual scenario adapter by default | effective factual outbound | factual outbound (A) | MIGRATED | execution state stays mutable; JSON/upload is explicit manual fallback |
| Current warehouse / maps / optimizer | placement | `load_placement_state[_cached]` | `placements.json` | mutations derived from factual initial state | DERIVED (B) | retain; never overwrite with historical snapshot |
| Inventory reconciliation legacy workspace | opening stock | `read_inventory_table` | manually uploaded workbook | factual inventory where contract matches | MANUAL COMPATIBILITY | independent mutable reconciliation retained |
| Actual inventory importer | START workbook | `read_actual_inventory_table` | bytes | historical placement | MANUAL ADAPTER | allowed only explicit fallback/tests |
| Day receipts importer | receipt workbook | `read_day_receipts_table` | bytes | factual receipts | MANUAL ADAPTER | allowed only explicit fallback/tests |
| Inventory-results importer | inventory control | `read_inventory_results_table` | bytes | factual inventory or independent selected control | MANUAL CONTROL | do not conflate silently |
| Geometry/model/gates/row constructor | geometry | geometry readers/model JSON | model/configuration | user configuration (C) | NOT FACTUAL | retain |
| Weight rules | thresholds | weight rules loader | `weight_zone_rules.json` | user configuration (C) | NOT FACTUAL | retain |
| Execution logs, snapshots, comparisons | results | persistence loaders | JSON artifacts | result artifacts (E) | NOT FACTUAL | retain |
| State caches / revision tokens | all | `warehouse_state_cache` | memory/file signatures | cache (D) | NOT FACTUAL | retain |

## Repository scan conclusions

Direct XLSX readers in factual import are the ingestion boundary. Readers in the three legacy importer modules and function-level allowlisted branches of `virtual_warehouse_app.py` are explicit manual/compatibility workflows. Geometry/row-construction Excel is a different business contract. Monthly replay already uses factual effective views. Routing, optimizer and scenario engines consume supplied states and do not independently load legacy factual artifacts. `placements.json` contains mutable placements, journals, proposed receipt placement and outbound mutations, so it must not be destructively migrated. Historical placement has no warehouse field: CURRENT requires an explicit user/model binding to the selected outbound warehouse and blocks when it is absent or different.
