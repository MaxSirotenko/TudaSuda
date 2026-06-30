from pathlib import Path
import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan2.dxf")
print("DXF =", DXF_PATH)
OUTPUT_PATH = Path("results/all_pallets.xlsx")

PALLET_SIZES = {
    (1200.0, 800.0),
    (800.0, 1200.0),
    (1000.0, 1200.0),
    (1200.0, 1000.0),
    (1200.0, 1200.0),
}

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

rows = []


def add_pallet(source, layer, block_name, min_x, min_y, max_x, max_y):
    width = round(abs(max_x - min_x), 2)
    height = round(abs(max_y - min_y), 2)

    if (width, height) not in PALLET_SIZES:
        return

    rows.append({
        "source": source,
        "layer": layer,
        "block_name": block_name,
        "x_min": round(min_x, 2),
        "y_min": round(min_y, 2),
        "x_max": round(max_x, 2),
        "y_max": round(max_y, 2),
        "center_x": round((min_x + max_x) / 2, 2),
        "center_y": round((min_y + max_y) / 2, 2),
        "width": width,
        "height": height,
    })


def handle_lwpolyline(entity, source, base_x=0, base_y=0, block_name=None):
    points = list(entity.get_points())

    if len(points) < 4:
        return

    xs = [p[0] + base_x for p in points]
    ys = [p[1] + base_y for p in points]

    add_pallet(
        source=source,
        layer=entity.dxf.layer,
        block_name=block_name,
        min_x=min(xs),
        min_y=min(ys),
        max_x=max(xs),
        max_y=max(ys),
    )


def explode_block(block_name, base_x=0, base_y=0, level=0):
    if block_name not in doc.blocks:
        return

    block = doc.blocks[block_name]

    for entity in block:
        entity_type = entity.dxftype()

        if entity_type == "INSERT":
            insert = entity.dxf.insert
            child_block = entity.dxf.name

            explode_block(
                child_block,
                base_x + insert.x,
                base_y + insert.y,
                level + 1,
            )

        elif entity_type == "LWPOLYLINE":
            handle_lwpolyline(
                entity,
                source="insert",
                base_x=base_x,
                base_y=base_y,
                block_name=block_name,
            )


print("Читаю LWPOLYLINE из modelspace...")

for entity in msp:
    if entity.dxftype() == "LWPOLYLINE":
        handle_lwpolyline(
            entity,
            source="modelspace",
            base_x=0,
            base_y=0,
            block_name=None,
        )

print("Разворачиваю INSERT...")

for entity in msp:
    if entity.dxftype() != "INSERT":
        continue

    insert = entity.dxf.insert
    block_name = entity.dxf.name

    explode_block(
        block_name,
        base_x=insert.x,
        base_y=insert.y,
        level=0,
    )

df = pd.DataFrame(rows)

print("До удаления дублей:", len(df))

# Удаляем дубли по центру и размеру.
# Округление до 1 мм, чтобы одинаковые объекты схлопнулись.
df["center_x_round"] = df["center_x"].round(0)
df["center_y_round"] = df["center_y"].round(0)
df["width_round"] = df["width"].round(0)
df["height_round"] = df["height"].round(0)

df = df.drop_duplicates(
    subset=[
        "center_x_round",
        "center_y_round",
        "width_round",
        "height_round",
    ]
).copy()

df = df.sort_values(["center_x", "center_y"]).reset_index(drop=True)
df["pallet_id"] = df.index + 1

print("После удаления дублей:", len(df))

sizes = (
    df.groupby(["width", "height"])
    .size()
    .reset_index(name="count")
    .sort_values("count", ascending=False)
)

print()
print("Размеры паллет:")
print(sizes)

Path("results").mkdir(exist_ok=True)

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="all_pallets")
    sizes.to_excel(writer, index=False, sheet_name="sizes")

print()
print("Сохранено:", OUTPUT_PATH)