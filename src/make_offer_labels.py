"""Hand-labelled offers: the ground truth for the extraction eval.

Usage:
    python src/make_offer_labels.py new <flyer_folder> <page> [<page> ...]   # blank label file per page
    python src/make_offer_labels.py check                                     # validate every label file

Files:
    data/labels/offers/<store>_<flyer>_p<NN>.csv    one row per offer on that page (you fill it in)
    data/labels/OFFERS_GUIDE.md                      what goes in each column

The label files contain no leaflet images, so they are committed.
Label pages BEFORE running the model on them, so its output can't anchor your labels.
"""

import csv
import sys
from pathlib import Path

OUT = Path("data/labels/offers")
COLUMNS = ["offer", "name", "brand", "size", "price", "price_per", "regular_price",
           "unit_price", "unit_price_per", "condition", "discount_text", "notes"]
PRICE_PER = {"unit", "kg", "l"}
CONDITIONS = {"none", "card", "app", "quantity", "other"}
NUMBER_FIELDS = ["price", "regular_price", "unit_price"]


def label_path(folder: Path, page: int) -> Path:
    store = folder.parent.name                 # data/raw/<store>/<flyer_id>
    return OUT / f"{store}_{folder.name[:8]}_p{page:02d}.csv"


def page_reference(folder: Path, page: int) -> str:
    """Text to copy product names from: Continente's printed page text, or Lidl's alt text and keywords."""
    with open(folder / "manifest.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if int(row["page"]) == page:
                return row.get("text") or f"{row.get('alt_text', '')}\nKeywords: {row.get('keywords', '')}"
    return ""


def cmd_new(folder: Path, pages: list[int]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for page in pages:
        path = label_path(folder, page)
        image = folder / f"page-{page:02d}.jpg"
        if not image.exists():
            print(f"skip page {page}: {image} not found")
            continue
        if path.exists():
            print(f"skip page {page}: {path} already exists (not overwriting your labels)")
            continue
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(COLUMNS)
        print(f"\n=== page {page} ===")
        print(f"label file: {path}")
        print(f"image:      {image}")
        print("text to copy names from (order is jumbled; the image is the truth):")
        print(page_reference(folder, page)[:3000])


def cmd_check() -> None:
    files = sorted(OUT.glob("*.csv"))
    if not files:
        sys.exit(f"No label files in {OUT}")
    problems, offers = 0, 0
    for path in files:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != COLUMNS:
                print(f"{path.name}: header changed; expected {COLUMNS}")
                problems += 1
                continue
            for i, row in enumerate(reader, start=2):
                if not any((v or "").strip() for v in row.values()):
                    continue                          # blank line
                offers += 1
                where = f"{path.name} line {i}"
                for field in NUMBER_FIELDS:
                    v = (row[field] or "").strip()
                    if v:
                        try:
                            float(v)
                        except ValueError:
                            print(f"{where}: {field}={v!r} is not a number (use a dot: 8.99)")
                            problems += 1
                if not (row["name"] or "").strip():
                    print(f"{where}: name is empty")
                    problems += 1
                if not (row["price"] or "").strip():
                    print(f"{where}: price is empty (offers without a concrete price are not labelled)")
                    problems += 1
                if (row["price_per"] or "").strip() not in PRICE_PER:
                    print(f"{where}: price_per={row['price_per']!r}, expected one of {sorted(PRICE_PER)}")
                    problems += 1
                up, upp = (row["unit_price"] or "").strip(), (row["unit_price_per"] or "").strip()
                if up and upp not in PRICE_PER:
                    print(f"{where}: unit_price given but unit_price_per={upp!r}")
                    problems += 1
                if (row["condition"] or "").strip() not in CONDITIONS:
                    print(f"{where}: condition={row['condition']!r}, expected one of {sorted(CONDITIONS)}")
                    problems += 1
    print(f"\n{len(files)} files, {offers} offers, {problems} problems")


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "new":
        cmd_new(Path(sys.argv[2].rstrip("/")), [int(p) for p in sys.argv[3:]])
    elif len(sys.argv) == 2 and sys.argv[1] == "check":
        cmd_check()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
