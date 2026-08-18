# Data source authority

This table is the production contract. Effective views in `warehouse_factual_data.py` are the only default source boundary; `warehouse_factual_scenario_inputs.py` adapts them for scenario engines. A factual conflict is a blocker and never triggers a legacy fallback.

| Data family | Authoritative source | Derived state | Explicit manual fallback | Consumers |
|---|---|---|---|---|
| Historical placement | Effective factual `historical_placement`, exact cell resolution | `placements.json` after user/model mutations | START workbook selected as **Manual fallback** | CURRENT baseline, FACT opening placement |
| Outbound | Effective factual `outbound`; `quantity = РасчетноеОтгруженоКоробок`; parser `factual-july-v5` | execution state/log and replay results | `outbound_orders.json` or workbook selected explicitly | CURRENT/PROPOSED, monthly FACT, route replay |
| Receipts | Effective factual `receipts` | classification/placement overlay keyed by canonical SKU/row identity | `receipts.json` or workbook selected explicitly | receipt zoning, day inputs, placement |
| Inventory | Effective factual `inventory` is quantity evidence only | reconciliation/comparison result | explicit inventory-control workbook with proven box calculation | diagnostics; never automatic box control from `actual_quantity` |
| VGH | Effective factual `vgh` | weight-zone result and user thresholds | receipt weight column only in explicit legacy workflow | receipt zoning and palletization |

## Invariants

* SKU identity remains `Номенклатура + Характеристика` through the shared business-identity boundary.
* Historical cells use exact authoritative resolution or an explicit persisted user mapping; ambiguous/unresolved cells block START.
* Historical placement has no warehouse column. A selected outbound warehouse is never copied onto it implicitly; an explicit dataset/model-to-warehouse confirmation is required.
* Manual/legacy sources are compatibility inputs, never an automatic empty/error fallback.
* One-day route order is resolved from the opening historical snapshot's unique `cell_picking_order`; outbound pick order is optional and missing/conflicting historical evidence blocks full-day authority.
* Scenario weight zones use relevant START/outbound SKU + factual VGH + persisted user bands; they do not require a receipt on D.
* Velocity history reads only `[D-28,D)` when the velocity rule is enabled and is cached by warehouse, day, and active outbound revision.
* The outbound-picking workspace is an operational mutation workflow: factual outbound is demand authority, while pick locations/order come only from current mutable placement execution. Historical route authority is reserved for CURRENT/PROPOSED and monthly FACT.
* Outbound, receipts and inventory conflicts are evaluated in the selected warehouse scope. Evidence with a missing warehouse remains a blocker because it cannot be assigned safely; a conflict spanning two warehouses relates to both scopes.
* Factual receipts require `document_ref`; unlike outbound, they have no confirmed number/date identity fallback. Identityless completed rows never enter mutable placement.
* Business-evidence indexes are derived, versioned by dataset identity/content and evidence semantics, and rebuilt from immutable canonical partitions only once when their signature changes.
* Operational-day evidence is persisted in day partitions; one-day and velocity queries never scan or materialize a month-wide evidence artifact.
* A completed receipt is placement-eligible only with a positive factual `reported_pallets`. Missing pallet quantity is a blocker; no unproven boxes-to-pallets conversion is applied.
* Operational factual outbound executes fail-closed: any source blocker empties executable demand while preserving diagnostics.
* Warehouse scope uses the shared canonical business-text normalizer; no adapter-specific or fuzzy warehouse matching is permitted.
* Evidence-index publication is serialized per artifact and uses unique staging/swap directories, so concurrent first access cannot expose a partial index.
* Route-location authority considers only positive opening stock and must belong to the shared physical graph's routable cell-access set.
* Completed receipts require canonical SKU identity; any completed invalid row blocks application of the entire factual receipt input.
* Mutable factual receipt identity is the authoritative `document_ref + line_number` business key; dataset/version/source-row values remain provenance only.
* Calculated factual receipt placements are warehouse-scoped. A new factual calculation retains prior receipt lines only from the same normalized warehouse; unknown provenance is excluded with diagnostics.
* Operational factual outbound may mutate placement only when the selected warehouse equals the explicit model factual warehouse binding; missing or mismatched binding fails closed.
* Active undated outbound evidence blocks only its proven warehouse scope, while missing warehouse scope blocks all potentially affected operational-day calculations.
* Outbound execution persistence remains global history, but factual screen summaries, line results and logs are projected to the selected day/warehouse order keys.
* Mutable placement, execution logs, model/geometry, gates, rules, render settings and comparison artifacts are derived state/configuration/results, not competing factual sources.
