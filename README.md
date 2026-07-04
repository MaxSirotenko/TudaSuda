# TudaSuda

Streamlit-проект для работы с картой/распознаванием и ручным конструктором рядов.

## Что входит

- `virtual_warehouse_app.py` — основное Streamlit-приложение виртуального склада Excel.
- `app.py` — совместимый wrapper для ручного запуска старой командой `streamlit run app.py`.
- `row_constructor.py` — конструктор рядов и виртуальных ячеек.
- `tools/` — вспомогательные скрипты для обработки DXF/Excel-данных.
- `requirements.txt` — все runtime-зависимости для приложений и утилит.


## Резолюция merge conflict в `app.py`

Если Git показывает конфликт между коротким wrapper и большим Streamlit-кодом в `app.py`,
выбирайте короткий wrapper. Полная реализация уже находится в `virtual_warehouse_app.py`;
возвращать её обратно в `app.py` не нужно. После резолюции `app.py` должен выглядеть так:

```python
from virtual_warehouse_app import main


main()
```

После сохранения проверьте, что в файлах не осталось реальных конфликтных блоков командой `git grep -n "^<<<<<<<\|^=======$\|^>>>>>>>" -- . || true`,
и запустите `python -m compileall .`.


## Резолюция merge conflict в `start.cmd`

Если Git показывает конфликт между запуском через `%STREAMLIT_ENTRYPOINT%` и запуском
через `app.py`, выбирайте вариант с `%STREAMLIT_ENTRYPOINT%`. Основной Streamlit-файл
теперь `virtual_warehouse_app.py`, а `app.py` — только wrapper для обратной совместимости.
В конфликте нужно оставить строки, которые:

- считают hash от `Path('%STREAMLIT_ENTRYPOINT%')`;
- пишут в лог `Streamlit entrypoint` и `Entrypoint file hash`;
- запускают `python -m streamlit run "%STREAMLIT_ENTRYPOINT%" ...`.

## Быстрый запуск на Windows

### Распознавалка

```bat
start.cmd
```

Скрипт сам перейдёт в папку проекта, безопасно попробует подтянуть свежий код из GitHub через `git fetch --prune` и `git pull --ff-only`, создаст `venv`, установит зависимости из `requirements.txt` только при изменении hash файла и запустит основной entrypoint `virtual_warehouse_app.py`:

```bat
streamlit run virtual_warehouse_app.py --server.address localhost --server.port 8501 --browser.serverAddress localhost
```

`app.py` оставлен только как compatibility wrapper для старых команд запуска. Полная Excel-only логика виртуального склада живёт в `virtual_warehouse_app.py`. После merge PR локально достаточно выполнить:

```bat
start.cmd
```

Подробности запуска, текущий commit, hash entrypoint и ошибки обновления пишутся в `start.log`. Если в рабочей папке есть локальные изменения, `start.cmd` не делает `pull`, чтобы ничего не затереть.

Если локальная папка уже конфликтная и вы точно готовы удалить локальные изменения, можно вручную синхронизироваться с GitHub:

```bash
git status
git fetch origin
git reset --hard origin/main
```

Внимание: `git reset --hard` безвозвратно удаляет локальные изменения в отслеживаемых файлах. Не запускайте его, если нужно сохранить свои правки.



Если браузер показывает `ERR_CONNECTION_REFUSED`, значит сервер Streamlit не запустился
или ещё устанавливает зависимости. Откройте окно `start.cmd` и дождитесь строки
`You can now view your Streamlit app in your browser`. Если `localhost` не открывается,
попробуйте прямой адрес <http://127.0.0.1:8501>.

Если после обновления проекта в браузере всё ещё виден старый интерфейс с разделами
`Шаблоны файлов`, `Загрузка данных`, `Карта РЦ`, `Карта склада`, `Расчет маршрутов`,
значит открыт старый Streamlit-процесс. Запускайте приложение через `start.cmd`: он

освобождает порт `8501`, пишет в `start.log` текущий git commit, entrypoint и hash `virtual_warehouse_app.py`, а затем
стартует актуальный `virtual_warehouse_app.py`. После запуска обновите страницу браузера.

### Конструктор рядов

```bat
start_row_constructor.cmd
```

Скрипт сам подготовит окружение и запустит:

```bat
streamlit run row_constructor.py --server.address localhost --server.port 8502 --browser.serverAddress localhost
```

Если `localhost:8502` не открывается, дождитесь запуска в окне
`start_row_constructor.cmd` или попробуйте <http://127.0.0.1:8502>.

## Быстрый запуск на macOS/Linux

### Распознавалка

```bash
./scripts/run_recognizer.sh
```

### Конструктор рядов

```bash
./scripts/run_row_constructor.sh
```

Оба скрипта создают `venv`, обновляют `pip`, устанавливают `requirements.txt` и запускают нужное Streamlit-приложение.

## Ручная установка

Если нужен полностью ручной запуск:

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Запуск распознавалки:

```bash
streamlit run virtual_warehouse_app.py --server.address localhost --server.port 8501 --browser.serverAddress localhost
```

Запуск конструктора рядов:

```bash
streamlit run row_constructor.py --server.address localhost --server.port 8502 --browser.serverAddress localhost
```


## Геометрическая модель склада из Excel

Основной режим страницы `virtual_warehouse_app.py` — **«Склад из Excel: ряды + ячейки + проезды»**. Он строит не пиксельную копию Excel-схемы, а рабочую модель склада в метрах:

- ячейки берутся только из Excel со списком фактических ячеек;
- поддерживаются колонки `Код`, `Ряд`, `Ячейка`, `Ярус` и варианты `Ряд / Ссылка`, `Ячейка / Ссылка`;
- при многоуровневом заголовке можно выбрать 1 или 2 строки заголовка;
- если автоопределение колонок не подошло, колонки выбираются вручную в интерфейсе;
- ряды строятся вертикально, ячейки идут снизу вверх по фактическому порядку номеров;
- размеры по умолчанию: ячейка `1.2 × 0.8 м`, межрядный проезд `3.4 м`, верхний и нижний проезд `3.4 м`;
- порядок рядов и проезды между рядами редактируются таблицами в интерфейсе;
- модель сохраняется в `data/last_import/warehouse_model.json` и подхватывается при следующем запуске.

## Импорт схемы склада из Excel

В конструкторе рядов есть блок **«Импорт из Excel»**:

- **Excel схемы рядов** — файл со строками рядов. Обязательные колонки: `Ряд` и `Кол-во ячеек` / `Количество ячеек`. Дополнительно можно указать `Склад`, `Часть ряда`, `Длина ячейки мм`, `Ширина ячейки мм`, `Зазор мм`, `Проезд мм`, `Поворот мм`, `Следующий ряд`, `Комментарий`.
- **Excel выгрузки 1С с ячейками** — файл с фактическими номерами/адресами ячеек. Обязательные колонки: `Ряд` и `Ячейка`. Дополнительно можно указать `Склад` и `Адрес ячейки` / `Складская ячейка`.

После загрузки схемы конструктор строит виртуальные ячейки по рядам и размерам. Если загружена выгрузка 1С, подписи и `cell_key` ячеек автоматически заменяются на адреса из 1С по совпадению `Склад + Ряд + Ячейка` или, если склад в выгрузке не указан, по совпадению `Ряд + Ячейка`.

## Зависимости

Основные зависимости описаны в `requirements.txt`:

- Streamlit UI: `streamlit`, `streamlit-image-coordinates`.
- Табличные данные и Excel: `pandas`, `openpyxl`.
- PDF/изображения: `PyMuPDF`, `Pillow`.
- DXF и диагностические утилиты: `ezdxf`, `matplotlib`.
