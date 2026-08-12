# July factual Data Contract audit

## Executable contract and evidence

The executable contract is split into `AUTHORITATIVE_CONTRACTS` and
`LEGACY_CONTRACT_ALIASES`. The authoritative spellings are the exact PR #162
contract. Legacy spellings are limited to inventory and receipts aliases that
PR #162 traces to the checked-in queries. Synthetic fixtures are not evidence.

| Source | Detection and mapping | Business key | Material conflict payload | Readiness role |
|---|---|---|---|---|
| historical placement | exact PR #162 columns; `Ячейка` remains `source_cell` evidence | no invented row key | multiple active sources for one snapshot are a conflict | D..D+1 snapshots and exact/user-confirmed geometry resolution are hard requirements |
| outbound | `СсылкаРО`, `НомерРО`, `ДатаРО`, `Склад`, `НомерСтроки`, SKU pair, `РасчетноеОтгруженоКоробок`, `ПорядокСборки` | `document_ref + line_number` | date, warehouse, SKU pair, calculated shipped boxes, pick order | positive demand and no conflicts are required |
| receipts | confirmed project columns plus separately marked query compatibility aliases | `document_ref + line_number` | date, warehouse, SKU pair, boxes, pallets and receipt controls | July availability and no conflicts are required |
| inventory | confirmed project columns plus separately marked query compatibility aliases | `inventory_ref + line_number` | date, warehouse, SKU pair, actual/accounting quantities | diagnostic, non-blocking when absent |
| VGH | exact confirmed columns | canonical `Номенклатура + Характеристика` SKU key | layer/pallet fields | missing/conflicting VGH remains a warning |

Canonical SKU identity continues to contain both nomenclature and
characteristic. Dates use explicit Russian day-first and ISO formats. Document
references are preserved as source values; no fuzzy document or cell matching
is performed.

## Root causes and history

PR #162 removed unproven outbound aliases and documented
`РасчетноеОтгруженоКоробок` as canonical quantity. Commit `e8878fa` (“Detect
outbound RO factual sources”), merged by PR #167, added `РасходныйОрдер`, generic
`Номер`, generic `Дата`, and generic `Количество` to factual detection to satisfy
a later source-detection change. The alias resolver selects the first present
spelling, so a workbook containing both `Количество = 24` and
`РасчетноеОтгруженоКоробок = 3` imported 24. No checked-in 1C query or real
contract evidence in that change justified redefining factual shipped boxes.
This was a code contract regression, not proof of bad Excel data.

The fix removes those four aliases from outbound authority. `Количество` is
still retained in the immutable RAW record and as `source_line_quantity_raw`
control evidence; canonical `quantity` comes only from
`РасчетноеОтгруженоКоробок`.

Filename is intentionally still part of logical source identity. Therefore
`РО июль.xlsx` and `РО июль(1).xlsx` may both remain active: automatically
superseding differently named sources is unsafe. Read-only Data Contract
diagnostics now reports both dataset IDs, filenames, periods, overlapping dates
and document keys, duplicate/conflict counts, and conflict provenance so the
user can explicitly choose which existing version to deactivate/supersede.

Historical addresses absent from local geometry remain a local-model mapping
problem (`historical_cell_unresolved`), not a source-schema error. This change
does not modify `warehouse_model.json`, generate row 152 cells, or introduce
fuzzy mapping.

## Parser compatibility and user action

Canonical semantics changed, so the parser version is `factual-july-v5`.
Active artifacts carrying an explicit older parser version are excluded at the
business-query boundary and produce the hard blocker `parser_reimport_required`.
They are never rewritten or interpreted as v5 artifacts.

After updating the application:

1. Reimport/reparse the already saved factual Excel files with v5; a new 1C
   export is not required when the original workbook is intact.
2. Open Data Contract diagnostics and verify mappings, active files, overlaps,
   parser compatibility, and conflict provenance.
3. Explicitly supersede/deactivate an unwanted filename variant using the
   existing lifecycle action; diagnostics itself is read-only.
4. Run July readiness. Resolve `historical_cell_unresolved` separately in the
   local geometry mapping if it remains.
5. Run monthly FACT only after readiness is green (VGH may remain a warning).

## 1C verification still required

The repository proves that `mass_outbound_orders.query` filters by dates,
warehouses, `ПометкаУдаления = ЛОЖЬ`, and `Проведен = ИСТИНА`. It does **not**
prove that “posted” is equivalent to the business rule “all required orders
except cancelled”. The query is therefore unchanged.

The minimal manual 1C verification is a small query/report over the July RO
document type returning document reference/number, `Проведен`,
`ПометкаУдаления`, and the configuration's actual status field/presentation for
cancelled and non-cancelled examples. Only after confirming the real metadata
field and enum values should the mass query be changed.
