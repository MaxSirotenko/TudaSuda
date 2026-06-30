from pathlib import Path
from collections import defaultdict
import math

import pandas as pd


IN_PATH = Path("results/manual_rows/pallets_numbered.xlsx")
OUT_PATH = Path("results/manual_rows/pallet_geometry_diagnostics.xlsx")

CENTER_ROUND_MM = 50
BBOX_ROUND_MM = 50

OVERLAP_RATIO_BAD = 0.80
OVERLAP_RATIO_WARN = 0.20

GRID_MM = 2500


def rect_area(r):
    return max(0, r["x_max"] - r["x_min"]) * max(0, r["y_max"] - r["y_min"])


def overlap_ratio(a, b):
    ix_min = max(a["x_min"], b["x_min"])
    ix_max = min(a["x_max"], b["x_max"])
    iy_min = max(a["y_min"], b["y_min"])
    iy_max = min(a["y_max"], b["y_max"])

    iw = max(0, ix_max - ix_min)
    ih = max(0, iy_max - iy_min)

    inter_area = iw * ih

    if inter_area <= 0:
        return 0

    area_a = rect_area(a)
    area_b = rect_area(b)

    min_area = min(area_a, area_b)

    if min_area <= 0:
        return 0

    return inter_area / min_area


def round_to(value, step):
    return round(float(value) / step) * step


def bucket_range(v_min, v_max, step):
    start = int(math.floor(v_min / step))
    end = int(math.floor(v_max / step))
    return range(start, end + 1)


def main():
    if not IN_PATH.exists():
        raise FileNotFoundError(f"Не найден файл: {IN_PATH}")

    df = pd.read_excel(IN_PATH)

    needed = ["pallet_id", "x_min", "x_max", "y_min", "y_max", "center_x", "center_y"]
    missing = [c for c in needed if c not in df.columns]

    if missing:
        raise ValueError(f"Не хватает колонок: {missing}. Есть колонки: {list(df.columns)}")

    df = df.copy()

    for col in ["x_min", "x_max", "y_min", "y_max", "center_x", "center_y"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["x_min", "x_max", "y_min", "y_max", "center_x", "center_y"]).copy()

    # Нормализуем min/max
    x1 = df[["x_min", "x_max"]].min(axis=1)
    x2 = df[["x_min", "x_max"]].max(axis=1)
    y1 = df[["y_min", "y_max"]].min(axis=1)
    y2 = df[["y_min", "y_max"]].max(axis=1)

    df["x_min"] = x1
    df["x_max"] = x2
    df["y_min"] = y1
    df["y_max"] = y2

    df["width"] = df["x_max"] - df["x_min"]
    df["height"] = df["y_max"] - df["y_min"]
    df["area"] = df["width"] * df["height"]

    df["center_key"] = df.apply(
        lambda r: (
            round_to(r["center_x"], CENTER_ROUND_MM),
            round_to(r["center_y"], CENTER_ROUND_MM),
        ),
        axis=1,
    )

    df["bbox_key"] = df.apply(
        lambda r: (
            round_to(r["x_min"], BBOX_ROUND_MM),
            round_to(r["x_max"], BBOX_ROUND_MM),
            round_to(r["y_min"], BBOX_ROUND_MM),
            round_to(r["y_max"], BBOX_ROUND_MM),
        ),
        axis=1,
    )

    duplicate_centers = (
        df.groupby("center_key", as_index=False)
        .agg(
            count=("pallet_id", "count"),
            pallet_ids=("pallet_id", lambda x: ", ".join(map(str, x))),
            x_avg=("center_x", "mean"),
            y_avg=("center_y", "mean"),
        )
        .query("count > 1")
        .sort_values("count", ascending=False)
    )

    duplicate_bboxes = (
        df.groupby("bbox_key", as_index=False)
        .agg(
            count=("pallet_id", "count"),
            pallet_ids=("pallet_id", lambda x: ", ".join(map(str, x))),
        )
        .query("count > 1")
        .sort_values("count", ascending=False)
    )

    records = df.to_dict("records")
    buckets = defaultdict(list)

    overlap_rows = []

    for i, rec in enumerate(records):
        bx_range = bucket_range(rec["x_min"], rec["x_max"], GRID_MM)
        by_range = bucket_range(rec["y_min"], rec["y_max"], GRID_MM)

        candidates = set()

        for bx in bx_range:
            for by in by_range:
                candidates.update(buckets.get((bx, by), []))

        for j in candidates:
            other = records[j]
            ratio = overlap_ratio(rec, other)

            if ratio >= OVERLAP_RATIO_WARN:
                overlap_rows.append(
                    {
                        "pallet_id_1": other["pallet_id"],
                        "pallet_id_2": rec["pallet_id"],
                        "overlap_ratio": ratio,
                        "level": "BAD" if ratio >= OVERLAP_RATIO_BAD else "WARN",
                        "x1": other["center_x"],
                        "y1": other["center_y"],
                        "x2": rec["center_x"],
                        "y2": rec["center_y"],
                        "w1": other["width"],
                        "h1": other["height"],
                        "w2": rec["width"],
                        "h2": rec["height"],
                    }
                )

        for bx in bx_range:
            for by in by_range:
                buckets[(bx, by)].append(i)

    overlaps = pd.DataFrame(overlap_rows)

    if not overlaps.empty:
        overlaps = overlaps.sort_values("overlap_ratio", ascending=False)

    summary = pd.DataFrame(
        [
            {"metric": "total_pallets", "value": len(df)},
            {"metric": "duplicate_center_groups", "value": len(duplicate_centers)},
            {
                "metric": "pallets_in_duplicate_centers",
                "value": int(duplicate_centers["count"].sum()) if not duplicate_centers.empty else 0,
            },
            {"metric": "duplicate_bbox_groups", "value": len(duplicate_bboxes)},
            {
                "metric": "pallets_in_duplicate_bboxes",
                "value": int(duplicate_bboxes["count"].sum()) if not duplicate_bboxes.empty else 0,
            },
            {
                "metric": "overlap_warn_pairs_20pct_plus",
                "value": len(overlaps) if not overlaps.empty else 0,
            },
            {
                "metric": "overlap_bad_pairs_80pct_plus",
                "value": int((overlaps["overlap_ratio"] >= OVERLAP_RATIO_BAD).sum()) if not overlaps.empty else 0,
            },
        ]
    )

    size_summary = (
        df.groupby(["width", "height"], as_index=False)
        .agg(count=("pallet_id", "count"))
        .sort_values("count", ascending=False)
    )

    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        duplicate_centers.to_excel(writer, sheet_name="duplicate_centers", index=False)
        duplicate_bboxes.to_excel(writer, sheet_name="duplicate_bboxes", index=False)
        overlaps.to_excel(writer, sheet_name="overlaps", index=False)
        size_summary.to_excel(writer, sheet_name="size_summary", index=False)
        df.to_excel(writer, sheet_name="pallets_checked", index=False)

    print()
    print(f"Паллетомест: {len(df)}")
    print(f"Групп с одинаковыми центрами: {len(duplicate_centers)}")
    print(f"Групп с одинаковыми bbox: {len(duplicate_bboxes)}")
    print(f"Пар с пересечением 20%+: {len(overlaps) if not overlaps.empty else 0}")

    if not overlaps.empty:
        bad_count = int((overlaps["overlap_ratio"] >= OVERLAP_RATIO_BAD).sum())
        print(f"Пар с пересечением 80%+: {bad_count}")
    else:
        print("Пар с пересечением 80%+: 0")

    print(f"Сохранено: {OUT_PATH}")


if __name__ == "__main__":
    main()