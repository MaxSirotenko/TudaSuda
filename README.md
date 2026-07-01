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
streamlit run app.py
```

Обычно приложение открывается по адресу <http://localhost:8501>.

### Конструктор рядов

```bat
start_row_constructor.cmd
```

Скрипт сам подготовит окружение и запустит:

```bat
streamlit run row_constructor.py --server.port 8502
```

Обычно конструктор открывается по адресу <http://localhost:8502>.

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
streamlit run app.py
```

Запуск конструктора рядов:

```bash
streamlit run row_constructor.py --server.port 8502
```

## Зависимости

Основные зависимости описаны в `requirements.txt`:

- Streamlit UI: `streamlit`, `streamlit-image-coordinates`.
- Табличные данные и Excel: `pandas`, `openpyxl`.
- PDF/изображения: `PyMuPDF`, `Pillow`.
- DXF и диагностические утилиты: `ezdxf`, `matplotlib`.
