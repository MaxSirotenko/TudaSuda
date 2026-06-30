from pathlib import Path
from collections import Counter

import ezdxf
import pandas as pd


DXF_PATH = Path("uploads/plan.dxf")


print("Открываю DXF...")

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

layers = Counter()
types = Counter()

rows = []

for entity in msp:

    entity_type = entity.dxftype()
    layer = entity.dxf.layer

    layers[layer] += 1
    types[entity_type] += 1

    rows.append({
        "layer": layer,
        "entity_type": entity_type
    })

print()
print("ТОП слоев:")
print("-" * 50)

for layer, count in layers.most_common(30):
    print(f"{layer:<40} {count}")

print()
print("ТОП типов объектов:")
print("-" * 50)

for entity_type, count in types.most_common():
    print(f"{entity_type:<20} {count}")

df = pd.DataFrame(rows)

Path("results").mkdir(exist_ok=True)

df.to_excel(
    "results/dxf_entities.xlsx",
    index=False
)

print()
print("Файл сохранен:")
print("results/dxf_entities.xlsx")