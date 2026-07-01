from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

INPUT_PATH = Path("results/all_pallets.xlsx")
OUTPUT_PATH = Path("results/all_pallets_plot.png")

df = pd.read_excel(INPUT_PATH, sheet_name="all_pallets")

plt.figure(figsize=(18, 10))
plt.scatter(df["center_x"], df["center_y"], s=1)

plt.title("Все паллетоместа из DXF")
plt.xlabel("X")
plt.ylabel("Y")
plt.axis("equal")
plt.grid(True)

plt.savefig(OUTPUT_PATH, dpi=300)

print("Паллет:", len(df))
print("Сохранено:", OUTPUT_PATH)