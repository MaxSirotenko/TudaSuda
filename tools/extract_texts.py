from pathlib import Path
import ezdxf
import pandas as pd

DXF_PATH = Path("uploads/plan2.dxf")

doc = ezdxf.readfile(DXF_PATH)
msp = doc.modelspace()

rows = []

for entity in msp:

    try:

        if entity.dxftype() == "TEXT":

            insert = entity.dxf.insert

            rows.append({
                "type": "TEXT",
                "text": entity.dxf.text,
                "x": insert.x,
                "y": insert.y,
                "layer": entity.dxf.layer
            })

        elif entity.dxftype() == "MTEXT":

            text = entity.text

            insert = entity.dxf.insert

            rows.append({
                "type": "MTEXT",
                "text": text,
                "x": insert.x,
                "y": insert.y,
                "layer": entity.dxf.layer
            })

    except:
        pass

df = pd.DataFrame(rows)

print()
print("Всего текстов:", len(df))

print()
print(df.head(100))

df.to_excel(
    "results/texts.xlsx",
    index=False
)

print()
print("Сохранено: results/texts.xlsx")