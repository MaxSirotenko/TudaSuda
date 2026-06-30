from pathlib import Path
import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan.dxf")
OUTPUT_PATH = Path("results/exploded_inserts.xlsx")

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

rows = []


def extract_block(block_name, base_x=0, base_y=0, level=0, parent="MODEL"):
    if block_name not in doc.blocks:
        return

    block = doc.blocks[block_name]

    for entity in block:
        entity_type = entity.dxftype()

        if entity_type == "INSERT":
            insert = entity.dxf.insert
            child_block = entity.dxf.name

            extract_block(
                child_block,
                base_x + insert.x,
                base_y + insert.y,
                level + 1,
                block_name
            )

        elif entity_type == "LWPOLYLINE":
            points = list(entity.get_points())

            if len(points) >= 4:
                xs = [p[0] + base_x for p in points]
                ys = [p[1] + base_y for p in points]

                min_x = min(xs)
                max_x = max(xs)
                min_y = min(ys)
                max_y = max(ys)

                width = round(abs(max_x - min_x), 2)
                height = round(abs(max_y - min_y), 2)

                rows.append({
                    "parent": parent,
                    "block_name": block_name,
                    "level": level,
                    "entity_type": entity_type,
                    "x_min": round(min_x, 2),
                    "y_min": round(min_y, 2),
                    "x_max": round(max_x, 2),
                    "y_max": round(max_y, 2),
                    "center_x": round((min_x + max_x) / 2, 2),
                    "center_y": round((min_y + max_y) / 2, 2),
                    "width": width,
                    "height": height,
                    "layer": entity.dxf.layer
                })


print("Разворачиваю INSERT из modelspace...")

for entity in msp:
    if entity.dxftype() != "INSERT":
        continue

    insert = entity.dxf.insert
    block_name = entity.dxf.name

    extract_block(
        block_name,
        insert.x,
        insert.y,
        0,
        "MODEL"
    )

df = pd.DataFrame(rows)

print("Найдено LWPOLYLINE внутри блоков:", len(df))

print()
print("ТОП размеров внутри INSERT:")
print(
    df.groupby(["width", "height"])
    .size()
    .reset_index(name="count")
    .sort_values("count", ascending=False)
    .head(50)
)

Path("results").mkdir(exist_ok=True)

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="exploded")
    (
        df.groupby(["width", "height"])
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
        .to_excel(writer, index=False, sheet_name="sizes")
    )

print()
print("Сохранено:", OUTPUT_PATH)