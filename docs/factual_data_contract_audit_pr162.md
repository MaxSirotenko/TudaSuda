# PR #162 factual source contract audit

Only exact spellings in this table participate in automatic mapping. Synthetic tests are not evidence.

| Source | Canonical field | Accepted source column(s) | Evidence | Status |
|---|---|---|---|---|
| historical_placement | snapshot_at / source_pallet_ref / nomenclature / characteristic / source_stock_quantity / cell / cell_picking_order / source_position_balance | `ДатаСреза`; `Паллета`; `Номенклатура`; `Характеристика`; `КоличествоОстатокТовара`; `Ячейка`; `ПорядокСборки`; `КоличествоОстатокПоложения` | confirmed `размещение июль.xlsx` contract in task | CONFIRMED_PROJECT_SOURCE |
| inventory | inventory_ref | `Ссылка`; `Инвентаризация`; `СсылкаИнвентаризации` | task contract; checked-in `queries_1c/inventory_results.query` | CONFIRMED_PROJECT_SOURCE / LEGACY_COMPATIBILITY |
| inventory | inventory_number / occurred_at | `Номер`; `Дата`; `НомерИнвентаризации`; `ДатаИнвентаризации` | task contract; checked-in query | CONFIRMED_PROJECT_SOURCE / LEGACY_COMPATIBILITY |
| inventory | line_number / warehouse / nomenclature / characteristic | `НомерСтроки`; `Склад`; `Номенклатура`; `Характеристика` | task contract and checked-in query | CONFIRMED_PROJECT_SOURCE |
| inventory | actual_quantity / accounting_quantity | `КоличествоФакт`; `КоличествоУчет`; `ФактическоеКоличество`; `УчетноеКоличество` | task contract; checked-in query output | CONFIRMED_PROJECT_SOURCE / LEGACY_COMPATIBILITY |
| receipts | document_ref / document_number / occurred_at | `Ссылка`; `Номер`; `Дата`; `СсылкаПриходногоОрдера`; `НомерПриходногоОрдера`; `ДатаПриходногоОрдера` | confirmed `ПО июль.xlsx`; checked-in `queries_1c/day_receipts.query` | CONFIRMED_PROJECT_SOURCE / LEGACY_COMPATIBILITY |
| receipts | warehouse / line_number / nomenclature / characteristic / box_quantity / reported_pallets / terminal_completed / expected_receipt | `Склад`; `НомерСтроки`; `Номенклатура`; `Характеристика`; `КоличествоКоробок`; `КоличествоПаллет`; `ПриемкаТерминаломЗакончена`; `ОжидаемыйПриход` | confirmed `ПО июль.xlsx` and checked-in query | CONFIRMED_PROJECT_SOURCE |
| outbound | document_ref / document_number / occurred_at | `СсылкаРО`; `НомерРО`; `ДатаРО` | `queries_1c/mass_outbound_orders.query` output aliases | EXISTING_WORKING_QUERY |
| outbound | warehouse / line_number / nomenclature / characteristic / quantity / source_pick_order | `Склад`; `НомерСтроки`; `Номенклатура`; `Характеристика`; `РасчетноеОтгруженоКоробок`; `ПорядокСборки` | checked-in query output aliases | EXISTING_WORKING_QUERY |
| vgh | nomenclature / characteristic / weight / length / width / height / boxes_per_layer / layers_per_pallet / quantity_per_box | `Номенклатура`; `Характеристика`; `Вес`; `Длина`; `Ширина`; `Высота`; `КоличествоКоробовВОдномСлоеНаПаллете`; `КоличествоСлоевНаПаллете`; `КоличествоВКоробке` | confirmed VGH source description in task | CONFIRMED_PROJECT_SOURCE |

## Quarantined PR #161 aliases

The following aliases had no applicable evidence for the factual monthly contract and were removed from authoritative detection: `СсылкаРасходногоОрдера`, `РасходныйОрдер`, `НомерРасходногоОрдера`, `ДатаРасходногоОрдера`, `ДатаСоздания`, `РасчетноеКоличествоКоробов`, and outbound `КоличествоКоробок`. Plausible-looking columns now remain `unknown_schema`/`mapping_required` and are shown diagnostically.

## Architecture notes

* A logical source slot is SHA-256 of normalized `(source_type, original filename, sheet)`. Content/parser identity remains immutable and separate.
* Import hashes before workbook parsing. Successful publication atomically renames a staging directory, then changes registry activation. A failed replacement cannot deactivate its predecessor.
* Import currently retains the pandas table plus RAW and canonical lists. Partition buffers only contain references, but true row-streaming is not yet implemented; peak memory remains linear and is a known limitation.
* Persisted `index` metadata contains SKU keys, dates, daily row/SKU/cell/document/positive-quantity statistics, and document keys. Coverage reads only SKU indexes; a day summary opens only that day's partitions and reads VGH/D+1 availability from indexes.
* No authoritative historical `Ячейка` to warehouse geometry resolver was found. The source cell is preserved and `historical_cell_not_resolved_to_geometry` is emitted; display strings and `cell_key` are not guessed equivalent.
