# PR #160 — аудит целостности production UI

База аудита: `3d22ed16b8fe9af556ee419606cab874283d9c75` (merge PR #159).
Remote в managed snapshot отсутствует; базой служит предоставленный `HEAD`.

## Таблица аудита

| UI location | Действие | Видимое ожидание | Владелец состояния | Потребитель | Обнаруженное расхождение | Severity | Исправление |
|---|---|---|---|---|---|---|---|
| Условия модели | включить правило | правило изменит PROPOSED | `workspace_rule_config` | `build_proposed_scenario` | comparison имел второй набор checkbox | critical | comparison показывает read-only summary и получает workspace RuleSet |
| Условия модели | изменить минимум мест | optimizer получает число | `base_sku_capacity.parameters` | optimizer | comparison сбрасывал значение в `1` | critical | параметр проходит через единый builder без перезаписи |
| Данные | выбрать день | replay использует день РО | outbound rows | demand builder | наличие receipts меняло владельца и список дат | critical | список дат строится только из РО выбранного склада; receipts — validation-only |
| Пробег | выбрать ворота | replay стартует из настроенных ворот | persistent model gates | physical graph/replay | UI создавал независимые X/Y `experiment_gate` | critical | разрешён выбор только из сохранённых gates; отсутствие направляет в Склад → Ворота |
| Stepper | увидеть «готово» | следующий action проходит readiness | authoritative readiness/signatures | scenario/benchmark | отсутствующие flags и model трактовались как ready | high | обязательные flags теперь принимаются только при явном `True` |
| CURRENT / PROPOSED | изменить RuleSet | старый result становится stale | scenario signature | scenario UI/stepper | workspace stale flags конкурировали с signature | high | stepper и analytics проецируют сохранённые/активные signatures |
| Аналитика | открыть headline | видны метрики полного дня | comparison `authoritative_summary` | analytics adapter | UI читал obsolete generic keys | critical | точное отображение picker distance, orders, picked, shortage, equivalence |
| Аналитика | открыть invalid result | savings скрыта | `full_day_effect_valid` | analytics UI | технические partial values могли выглядеть как эффект | high | headline отсутствует; показан actionable blocker |
| Пробег | выбрать РО | виден рассчитанный путь | replay `route_legs` + graph | route UI | route helper не был подключён production path | high | replay сохраняет использованный graph, UI строит overlay только из replay evidence |
| Данные | получить scope error | понятно, что исправить | backend codes | message catalog | raw technical code был primary text | high | WHAT/WHY/ACTION + code в secondary expander |
| Данные | не загрузить END/receipts/inventory | V1 остаётся доступным | optional import state | benchmark | optional receipts владели date contract | high | optional inputs не участвуют в обязательной readiness |

## Единственные владельцы

* **RuleSet:** `workspace_rule_config`, редактируется только в «Условия модели».
* **START / РО / склад / дата:** operational data adapter; comparison получает уже выбранный scope.
* **Ворота:** `workspace_gate_state`, настраиваемый в «Склад → Ворота» (с совместимым чтением `model.gates`); независимые координаты benchmark удалены.
* **PROPOSED stale:** comparison saved signature против active signature.
* **Benchmark/analytics stale:** saved distance signature против active distance signature.
* **Маршрут:** числовой replay и использованный им physical graph; UI не ищет новый путь.

Persisted receipts и прочие legacy datasets сохранены для совместимости и классификационных
evidence. Они не выбирают дату и ворота и не переопределяют видимый RuleSet.

## Ручной trace одного сценария

| Шаг | Control | Авторитетная запись | Потребитель | Blocker | Stale |
|---|---|---|---|---|---|
| 1 | редакторы Склад | geometry model + persistent gates | optimizer/physical graph | invalid geometry/gate | geometry → PROPOSED + benchmark |
| 2 | START uploader | factual placement state | baseline builder | missing/not-business-ready START | START → PROPOSED + benchmark |
| 3 | RO source/uploader | normalized outbound rows | demand builder | missing rows/pick sequence | demand → benchmark |
| 4 | warehouse/date selects | exact V1 scope | baseline + demand filters | mismatch/no accepted RO | scope → benchmark (и baseline change → PROPOSED) |
| 5 | RuleSet controls | `workspace_rule_config` | scenario rule config | invalid dependency | RuleSet → PROPOSED + benchmark |
| 6 | Пересчитать PROPOSED | scenario + saved signature | maps/benchmark | scenario diagnostics | clears PROPOSED stale |
| 7 | Рассчитать пробег | replay/comparison + distance signature | route/analytics | gate/route/service | clears benchmark stale |
| 8 | RO для просмотра | presentation-only key | route overlay | missing route evidence warning | none |
| 9 | Аналитика | read-only cached comparison | headline adapter | stale/invalid full day | none |
| 10 | изменить правило | workspace RuleSet | active scenario signature | PROPOSED stale | PROPOSED + benchmark |
| 11 | повторный расчёт | new scenario/replay | maps/routes/analytics | same authoritative blockers | clears matching stale states |

## Намеренно сохранённые ограничения

V1 по-прежнему моделирует `opening factual START → outbound ROs`; intraday receipts не
replay-ятся. Алгоритмы placement, route и replay, thresholds и SKU semantics не изменялись.
