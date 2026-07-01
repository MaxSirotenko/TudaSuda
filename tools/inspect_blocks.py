from pathlib import Path
from collections import Counter
import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan.dxf")
OUTPUT_PATH = Path("results/block_contents.xlsx")

doc = ezdxf.readfile(DXF_PATH)

target_blocks = [
    "*U318",
    "___2",
    "TYPE1_FR_K",
    "PALET_1_K",
    "BALKA_K",
]

rows = []

for block_name in target_blocks:
    if block_name not in doc.blocks:
        print("Нет блока:", block_name)
        continue

    block = doc.blocks[block_name]
    counter = Counter()

    for entity in block:
        entity_type = entity.dxftype()
        layer = entity.dxf.layer
        counter[entity_type] += 1

        rows.append({
            "block_name": block_name,
            "entity_type": entity_type,
            "layer": layer
        })

    print()
    print("Блок:", block_name)
    print(counter)

df = pd.DataFrame(rows)

summary = (
    df.groupby(["block_name", "entity_type", "layer"])
    .size()
    .reset_index(name="count")
    .sort_values(["block_name", "count"], ascending=[True, False])
)

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="raw")
    summary.to_excel(writer, index=False, sheet_name="summary")

print()
print("Сохранено:", OUTPUT_PATH)