from pathlib import Path
from collections import Counter

import ezdxf
import pandas as pd

UPLOADS_DIR = Path("uploads")
OUTPUT_PATH = Path("results/dxf_compare.xlsx")

PALLET_SIZES = {
    (1200.0, 800.0),
    (800.0, 1200.0),
    (1000.0, 1200.0),
    (1200.0, 1000.0),
    (1200.0, 1200.0),
}

rows = []
layer_rows = []
type_rows = []
text_rows = []

for dxf_path in sorted(UPLOADS_DIR.glob("*.dxf")):
    print("Читаю:", dxf_path)

    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()

        entity_types = Counter()
        layers = Counter()

        text_count = 0
        insert_count = 0
        lwpolyline_count = 0
        pallet_candidates = 0

        for entity in msp:
            entity_type = entity.dxftype()
            layer = entity.dxf.layer

            entity_types[entity_type] += 1
            layers[layer] += 1

            if entity_type == "INSERT":
                insert_count += 1

            if entity_type == "LWPOLYLINE":
                lwpolyline_count += 1

                try:
                    points = list(entity.get_points())

                    if len(points) >= 4:
                        xs = [p[0] for p in points]
                        ys = [p[1] for p in points]

                        width = round(abs(max(xs) - min(xs)), 2)
                        height = round(abs(max(ys) - min(ys)), 2)

                        if (width, height) in PALLET_SIZES:
                            pallet_candidates += 1
                except Exception:
                    pass

            if entity_type in ["TEXT", "MTEXT"]:
                text_count += 1

                try:
                    if entity_type == "TEXT":
                        text = entity.dxf.text
                        point = entity.dxf.insert
                    else:
                        text = entity.text
                        point = entity.dxf.insert

                    text_rows.append({
                        "file": dxf_path.name,
                        "entity_type": entity_type,
                        "text": text,
                        "x": point.x,
                        "y": point.y,
                        "layer": layer,
                    })
                except Exception:
                    pass

        rows.append({
            "file": dxf_path.name,
            "total_entities": sum(entity_types.values()),
            "text_count": text_count,
            "insert_count": insert_count,
            "lwpolyline_count": lwpolyline_count,
            "pallet_candidates_modelspace": pallet_candidates,
            "layers_count": len(layers),
        })

        for entity_type, count in entity_types.items():
            type_rows.append({
                "file": dxf_path.name,
                "entity_type": entity_type,
                "count": count,
            })

        for layer, count in layers.items():
            layer_rows.append({
                "file": dxf_path.name,
                "layer": layer,
                "count": count,
            })

    except Exception as e:
        rows.append({
            "file": dxf_path.name,
            "error": str(e),
        })

summary_df = pd.DataFrame(rows)
types_df = pd.DataFrame(type_rows)
layers_df = pd.DataFrame(layer_rows)
texts_df = pd.DataFrame(text_rows)

OUTPUT_PATH.parent.mkdir(exist_ok=True)

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    summary_df.to_excel(writer, index=False, sheet_name="summary")
    types_df.to_excel(writer, index=False, sheet_name="entity_types")
    layers_df.to_excel(writer, index=False, sheet_name="layers")
    texts_df.to_excel(writer, index=False, sheet_name="texts")

print()
print(summary_df)
print()
print("Сохранено:", OUTPUT_PATH)