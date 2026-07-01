from pathlib import Path
import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan2.dxf")
OUTPUT_PATH = Path("results/pallets.xlsx")

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

rows = []

for entity in msp:
    if entity.dxftype() != "LWPOLYLINE":
        continue

    points = list(entity.get_points())

    if len(points) < 4:
        continue

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)

    width = round(abs(max_x - min_x), 2)
    height = round(abs(max_y - min_y), 2)

    if width == 1200 and height == 800:
        rows.append({
            "pallet_id": len(rows) + 1,
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

df = pd.DataFrame(rows)

Path("results").mkdir(exist_ok=True)

df.to_excel(OUTPUT_PATH, index=False)

print("Паллет найдено:", len(df))
print("Сохранено:", OUTPUT_PATH)
print(df.head())