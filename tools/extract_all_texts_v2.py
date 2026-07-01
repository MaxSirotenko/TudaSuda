from pathlib import Path
import re

import ezdxf
import pandas as pd


DXF_PATH = Path("uploads/plan2.dxf")
OUT_PATH = Path("results/all_texts_v2.xlsx")


BUSINESS_RANGES = [
    (0, 16, "Бакалея"),
    (17, 34, "Дневной и напитки"),
    (186, 213, "Долгосрок"),
    (214, 215, "Жуки и буфер"),
    (216, 223, "ДС Вешки"),
]


def clean_text(value: str) -> str:
    if value is None:
        return ""

    value = str(value)

    value = value.replace("\\P", " ")
    value = value.replace("\\~", " ")
    value = value.replace("\n", " ")
    value = value.replace("\r", " ")

    value = re.sub(r"[{}]", "", value)
    value = re.sub(r"\\[A-Za-z0-9_.|;,-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def parse_row_number(text: str):
    text = clean_text(text)

    if re.fullmatch(r"\d{1,3}", text):
        number = int(text)
        if 0 <= number <= 223:
            return number

    return None


def detect_business_zone(number):
    if pd.isna(number):
        return ""

    number = int(number)

    for start, end, name in BUSINESS_RANGES:
        if start <= number <= end:
            return name

    return "Вне ключевых диапазонов"


def get_insert_point(entity):
    try:
        point = entity.dxf.insert
        return float(point.x), float(point.y), float(point.z)
    except Exception:
        return None, None, None


def get_str_attr(entity, attr_name):
    try:
        value = getattr(entity.dxf, attr_name)
        if value is None:
            return ""
        return str(value)
    except Exception:
        return ""


def get_float_attr(entity, attr_name):
    try:
        value = getattr(entity.dxf, attr_name)
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def get_text_value(entity):
    dxftype = entity.dxftype()

    if dxftype == "TEXT":
        return entity.dxf.text

    if dxftype == "MTEXT":
        try:
            return entity.plain_text()
        except Exception:
            return entity.text

    if dxftype == "ATTRIB":
        return entity.dxf.text

    if dxftype == "ATTDEF":
        return entity.dxf.text

    return ""


def add_text_row(rows, entity, source, block_path=""):
    raw_text = get_text_value(entity)
    text = clean_text(raw_text)

    if not text:
        return

    x, y, z = get_insert_point(entity)

    tag = ""
    if entity.dxftype() == "ATTRIB":
        tag = get_str_attr(entity, "tag")

    rows.append(
        {
            "source": source,
            "type": entity.dxftype(),
            "text_raw": raw_text,
            "text": text,
            "row_number": parse_row_number(text),
            "layer": get_str_attr(entity, "layer"),
            "tag": tag,
            "x": x,
            "y": y,
            "z": z,
            "height": get_float_attr(entity, "height"),
            "rotation": get_float_attr(entity, "rotation"),
            "block_path": block_path,
            "handle": get_str_attr(entity, "handle"),
        }
    )


def walk_entity(entity, rows, errors, block_path="", depth=0):
    dxftype = entity.dxftype()

    if dxftype in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
        add_text_row(rows, entity, source="entity", block_path=block_path)
        return

    if dxftype == "INSERT":
        try:
            block_name = entity.dxf.name
        except Exception:
            block_name = "UNKNOWN_BLOCK"

        next_block_path = f"{block_path}/{block_name}" if block_path else block_name

        # ВАЖНО: достаём атрибуты самого INSERT
        try:
            for attrib in entity.attribs:
                add_text_row(
                    rows,
                    attrib,
                    source="insert_attrib",
                    block_path=next_block_path,
                )
        except Exception as e:
            errors.append(
                {
                    "block_path": next_block_path,
                    "depth": depth,
                    "error": f"ATTRIB error: {e}",
                }
            )

        # Разворачиваем содержимое блока
        try:
            for virtual_entity in entity.virtual_entities():
                walk_entity(
                    virtual_entity,
                    rows,
                    errors,
                    block_path=next_block_path,
                    depth=depth + 1,
                )
        except Exception as e:
            errors.append(
                {
                    "block_path": next_block_path,
                    "depth": depth,
                    "error": f"virtual_entities error: {e}",
                }
            )


def main():
    if not DXF_PATH.exists():
        raise FileNotFoundError(f"Не найден DXF: {DXF_PATH}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"DXF = {DXF_PATH}")
    print("Читаю файл...")

    doc = ezdxf.readfile(DXF_PATH)
    msp = doc.modelspace()

    rows = []
    errors = []

    print("Ищу TEXT / MTEXT / ATTRIB / ATTDEF...")

    for entity in msp:
        walk_entity(entity, rows, errors)

    df = pd.DataFrame(rows)

    if df.empty:
        print("Текст не найден.")
        return

    df["business_zone"] = df["row_number"].apply(detect_business_zone)

    candidates = df[df["row_number"].notna()].copy()
    candidates["row_number"] = candidates["row_number"].astype(int)
    candidates = candidates.sort_values(["row_number", "x", "y"])

    by_number = (
        candidates
        .groupby(["row_number", "business_zone"], as_index=False)
        .agg(
            count=("text", "count"),
            layers=("layer", lambda x: ", ".join(sorted(set(str(v) for v in x if pd.notna(v))))),
            sources=("source", lambda x: ", ".join(sorted(set(str(v) for v in x if pd.notna(v))))),
            tags=("tag", lambda x: ", ".join(sorted(set(str(v) for v in x if str(v).strip())))),
            x_min=("x", "min"),
            x_max=("x", "max"),
            y_min=("y", "min"),
            y_max=("y", "max"),
        )
        .sort_values("row_number")
    )

    by_layer = (
        df
        .groupby(["layer", "type", "source"], as_index=False)
        .agg(
            count=("text", "count"),
            sample_text=("text", lambda x: " | ".join(list(map(str, x.head(10))))),
        )
        .sort_values("count", ascending=False)
    )

    errors_df = pd.DataFrame(errors)

    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="all_texts", index=False)
        candidates.to_excel(writer, sheet_name="row_candidates", index=False)
        by_number.to_excel(writer, sheet_name="by_number", index=False)
        by_layer.to_excel(writer, sheet_name="by_layer", index=False)
        errors_df.to_excel(writer, sheet_name="errors", index=False)

    print()
    print(f"Всего текстов найдено: {len(df)}")
    print(f"Кандидатов на номера рядов 0-223: {len(candidates)}")
    print(f"Уникальных номеров рядов: {candidates['row_number'].nunique() if not candidates.empty else 0}")
    print(f"Ошибок при разборе INSERT: {len(errors_df)}")
    print(f"Сохранено: {OUT_PATH}")


if __name__ == "__main__":
    main()