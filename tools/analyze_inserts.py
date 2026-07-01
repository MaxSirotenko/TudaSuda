from pathlib import Path
from collections import Counter

import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan.dxf")
OUTPUT_PATH = Path("results/inserts_analysis.xlsx")

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

rows = []
block_counter = Counter()
layer_counter = Counter()

for entity in msp:
    if entity.dxftype() != "INSERT":
        continue

    block_name = entity.dxf.name
    layer = entity.dxf.layer
    insert = entity.dxf.insert

    xscale = getattr(entity.dxf, "xscale", 1)
    yscale = getattr(entity.dxf, "yscale", 1)
    rotation = getattr(entity.dxf, "rotation", 0)

    rows.append({
        "block_name": block_name,
        "layer": layer,
        "insert_x": insert.x,
        "insert_y": insert.y,
        "xscale": xscale,
        "yscale": yscale,
        "rotation": rotation
    })

    block_counter[block_name] += 1
    layer_counter[layer] += 1

df = pd.DataFrame(rows)

blocks_df = (
    df.groupby("block_name")
    .agg(
        count=("block_name", "count"),
        min_x=("insert_x", "min"),
        max_x=("insert_x", "max"),
        min_y=("insert_y", "min"),
        max_y=("insert_y", "max"),
    )
    .reset_index()
    .sort_values("count", ascending=False)
)

layers_df = (
    df.groupby("layer")
    .agg(
        count=("layer", "count"),
        min_x=("insert_x", "min"),
        max_x=("insert_x", "max"),
        min_y=("insert_y", "min"),
        max_y=("insert_y", "max"),
    )
    .reset_index()
    .sort_values("count", ascending=False)
)

Path("results").mkdir(exist_ok=True)

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="inserts")
    blocks_df.to_excel(writer, index=False, sheet_name="blocks")
    layers_df.to_excel(writer, index=False, sheet_name="layers")

print("INSERT всего:", len(df))
print()
print("ТОП блоков:")
print(blocks_df.head(30))
print()
print("ТОП слоев INSERT:")
print(layers_df.head(30))
print()
print("Сохранено:", OUTPUT_PATH)