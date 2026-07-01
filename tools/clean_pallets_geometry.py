from pathlib import Path
from collections import defaultdict
import math

import pandas as pd


IN_PATH = Path("results/manual_rows/pallets_numbered.xlsx")
OUT_PATH = Path("results/manual_rows/pallets_cleaned.xlsx")
REPORT_PATH = Path("results/manual_rows/pallets_cleaning_report.xlsx")

BBOX_ROUND_MM = 50

GRID_MM = 2500
BAD_OVERLAP_RATIO = 0.80

# Автоудаляем только почти одинаковые паллеты:
# сильное наложение + почти одинаковый центр + почти одинаковый размер
CENTER_DISTANCE_DUP_MM = 300
SIZE_DIFF_RATIO_DUP = 0.10


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


def center_distance(a, b):
    dx = a["center_x"] - b["center_x"]
    dy = a["center_y"] - b["center_y"]
    return math.sqrt(dx * dx + dy * dy)


def size_diff_ratio(a, b):
    area_a = rect_area(a)
    area_b = rect_area(b)

    max_area = max(area_a, area_b)

    if max_area <= 0:
        return 1

    return abs(area_a - area_b) / max_area


def round_to(value, step):
    return round(float(value) / step) * step


def bucket_range(v_min, v_max, step):
    start = int(math.floor(v_min / step))
    end = int(math.floor(v_max / step))
    return range(start, end + 1)


def normalize_geometry(df):
    result = df.copy()

    needed = ["x_min", "x_max", "y_min", "y_max"]
    missing = [c for c in needed if c not in result.columns]

    if missing:
        raise ValueError(f"Не хватает колонок: {missing}. Есть: {list(result.columns)}")

    for col in ["x_min", "x_max", "y_min", "y_max"]:
        result[col] = pd.to_numeric(result[col], errors="coerce")

    result = result.dropna(subset=["x_min", "x_max", "y_min", "y_max"]).copy()

    x1 = result[["x_min", "x_max"]].min(axis=1)
    x2 = result[["x_min", "x_max"]].max(axis=1)
    y1 = result[["y_min", "y_max"]].min(axis=1)
    y2 = result[["y_min", "y_max"]].max(axis=1)

    result["x_min"] = x1
    result["x_max"] = x2
    result["y_min"] = y1
    result["y_max"] = y2

    result["center_x"] = (result["x_min"] + result["x_max"]) / 2
    result["center_y"] = (result["y_min"] + result["y_max"]) / 2
    result["width"] = result["x_max"] - result["x_min"]
    result["height"] = result["y_max"] - result["y_min"]
    result["area"] = result["width"] * result["height"]

    return result


def add_bbox_key(df):
    result = df.copy()

    result["bbox_key"] = result.apply(
        lambda r: (
            round_to(r["x_min"], BBOX_ROUND_MM),
            round_to(r["x_max"], BBOX_ROUND_MM),
            round_to(r["y_min"], BBOX_ROUND_MM),
            round_to(r["y_max"], BBOX_ROUND_MM),
        ),
        axis=1,
    )

    return result


def find_overlaps(df):
    records = df.to_dict("records")
    buckets = defaultdict(list)

    rows = []

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

            if ratio >= BAD_OVERLAP_RATIO:
                dist = center_distance(rec, other)
                size_diff = size_diff_ratio(rec, other)

                rows.append(
                    {
                        "pallet_id_1": other.get("pallet_id"),
                        "pallet_id_2": rec.get("pallet_id"),
                        "overlap_ratio": ratio,
                        "center_distance": dist,
                        "size_diff_ratio": size_diff,
                        "area_1": rect_area(other),
                        "area_2": rect_area(rec),
                        "x1": other["center_x"],
                        "y1": other["center_y"],
                        "x2": rec["center_x"],
                        "y2": rec["center_y"],
                        "auto_duplicate": (
                            ratio >= BAD_OVERLAP_RATIO
                            and dist <= CENTER_DISTANCE_DUP_MM
                            and size_diff <= SIZE_DIFF_RATIO_DUP
                        ),
                    }
                )

        for bx in bx_range:
            for by in by_range:
                buckets[(bx, by)].append(i)

    overlaps = pd.DataFrame(rows)

    if not overlaps.empty:
        overlaps = overlaps.sort_values(
            ["auto_duplicate", "overlap_ratio", "center_distance"],
            ascending=[False, False, True],
        )

    return overlaps


def main():
    if not IN_PATH.exists():
        raise FileNotFoundError(f"Не найден файл: {IN_PATH}")

    df = pd.read_excel(IN_PATH)
    df = normalize_geometry(df)
    df = add_bbox_key(df)

    before_count = len(df)

    duplicate_groups = (
        df.groupby("bbox_key", as_index=False)
        .agg(
            count=("bbox_key", "count"),
            pallet_ids=("pallet_id", lambda x: ", ".join(map(str, x))),
        )
        .query("count > 1")
        .sort_values("count", ascending=False)
    )

    # 1. Удаляем точные/почти точные дубли bbox
    df_no_bbox_dups = df.drop_duplicates(subset=["bbox_key"], keep="first").copy()

    after_bbox_dedup_count = len(df_no_bbox_dups)

    # 2. Находим сильные пересечения после удаления bbox-дублей
    overlaps_after_bbox = find_overlaps(df_no_bbox_dups)

    # 3. Автоудаляем только почти одинаковые сущности:
    # сильное наложение + близкий центр + близкий размер.
    to_remove = set()

    if not overlaps_after_bbox.empty:
        auto_dups = overlaps_after_bbox[overlaps_after_bbox["auto_duplicate"] == True].copy()

        for _, row in auto_dups.iterrows():
            p1 = row["pallet_id_1"]
            p2 = row["pallet_id_2"]

            # Удаляем больший ID, чтобы результат был стабильным
            if pd.notna(p1) and pd.notna(p2):
                to_remove.add(max(int(p1), int(p2)))

    cleaned = df_no_bbox_dups[~df_no_bbox_dups["pallet_id"].isin(to_remove)].copy()

    # 4. Перенумеровываем pallet_id заново, чтобы не было дыр
    cleaned = cleaned.sort_values(
        ["center_x", "center_y"],
        ascending=[True, True],
    ).copy()

    cleaned = cleaned.drop(columns=["pallet_id"], errors="ignore")
    cleaned.insert(0, "pallet_id", range(0, len(cleaned)))

    # 5. Контроль после чистки
    cleaned_for_check = add_bbox_key(normalize_geometry(cleaned))
    overlaps_after_clean = find_overlaps(cleaned_for_check)

    summary = pd.DataFrame(
        [
            {"metric": "before_count", "value": before_count},
            {"metric": "after_bbox_dedup_count", "value": after_bbox_dedup_count},
            {"metric": "removed_by_bbox_duplicates", "value": before_count - after_bbox_dedup_count},
            {"metric": "auto_overlap_duplicates_to_remove", "value": len(to_remove)},
            {"metric": "final_cleaned_count", "value": len(cleaned)},
            {
                "metric": "bad_overlaps_after_bbox_dedup",
                "value": len(overlaps_after_bbox) if not overlaps_after_bbox.empty else 0,
            },
            {
                "metric": "bad_overlaps_after_clean",
                "value": len(overlaps_after_clean) if not overlaps_after_clean.empty else 0,
            },
        ]
    )

    removed_by_overlap = pd.DataFrame(
        [{"removed_pallet_id": x} for x in sorted(to_remove)]
    )

    with pd.ExcelWriter(REPORT_PATH, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        duplicate_groups.to_excel(writer, sheet_name="bbox_duplicate_groups", index=False)
        overlaps_after_bbox.to_excel(writer, sheet_name="overlaps_after_bbox", index=False)
        removed_by_overlap.to_excel(writer, sheet_name="removed_by_overlap", index=False)
        overlaps_after_clean.to_excel(writer, sheet_name="overlaps_after_clean", index=False)

    cleaned.to_excel(OUT_PATH, index=False)

    print()
    print(f"Было паллетомест: {before_count}")
    print(f"После удаления bbox-дублей: {after_bbox_dedup_count}")
    print(f"Удалено bbox-дублей: {before_count - after_bbox_dedup_count}")
    print(f"Удалено почти одинаковых наложений: {len(to_remove)}")
    print(f"Итог после чистки: {len(cleaned)}")

    if not overlaps_after_clean.empty:
        print(f"Сильных пересечений 80%+ после чистки: {len(overlaps_after_clean)}")
    else:
        print("Сильных пересечений 80%+ после чистки: 0")

    print(f"Чистый файл: {OUT_PATH}")
    print(f"Отчёт: {REPORT_PATH}")


if __name__ == "__main__":
    main()