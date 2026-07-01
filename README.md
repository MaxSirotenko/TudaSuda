# TudaSuda

Streamlit-проект для работы с картой/распознаванием и ручным конструктором рядов.

## Что входит

- `app.py` — основное приложение / распознавалка.
- `row_constructor.py` — конструктор рядов и виртуальных ячеек.
- `tools/` — вспомогательные скрипты для обработки DXF/Excel-данных.
- `requirements.txt` — все runtime-зависимости для приложений и утилит.

## Быстрый запуск на Windows

### Распознавалка

```bat
start.cmd
```

Скрипт сам перейдёт в папку проекта, создаст `venv`, установит зависимости из `requirements.txt` и запустит:

```bat
streamlit run app.py --server.address localhost --server.port 8501 --browser.serverAddress localhost
```



Если браузер показывает `ERR_CONNECTION_REFUSED`, значит сервер Streamlit не запустился
или ещё устанавливает зависимости. Откройте окно `start.cmd` и дождитесь строки
`You can now view your Streamlit app in your browser`. Если `localhost` не открывается,
попробуйте прямой адрес <http://127.0.0.1:8501>.

### Конструктор рядов

```bat
start_row_constructor.cmd
```

Скрипт сам подготовит окружение и запустит:

```bat
streamlit run row_constructor.py --server.address localhost --server.port 8502 --browser.serverAddress localhost
```

```bat
streamlit run app.py

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
streamlit run app.py --server.address localhost --server.port 8501 --browser.serverAddress localhost
```

Запуск конструктора рядов:

```bash
streamlit run row_constructor.py --server.address localhost --server.port 8502 --browser.serverAddress localhost
```

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
