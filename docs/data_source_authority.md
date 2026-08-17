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
* Mutable placement, execution logs, model/geometry, gates, rules, render settings and comparison artifacts are derived state/configuration/results, not competing factual sources.
