"""Decide which leaflet pages are worth sending to the vision model (zero LLM cost).

A page is "relevant" if it could contain something from a weekly shopping list:
food, drinks, household consumables (cleaning, paper), personal care, pet food.
Not relevant: tools, clothing, plants, decor, appliances, institutional pages.

Missing a relevant page (false negative) is worse than keeping an extra page
(false positive costs one extra API call), so recall is the number that matters.

Usage:
    python src/classify_pages.py template <flyer_folder>   # blank file for YOUR hand labels (do this first)
    python src/classify_pages.py auto <flyer_folder>       # keyword-rule predictions
    python src/classify_pages.py eval <flyer_folder>       # compare predictions with your labels

Files (labels contain no leaflet images, so they can be committed):
    data/labels/pages_<flyer_id>_gold.csv   your hand labels: relevant = 1 or 0
    data/labels/pages_<flyer_id>_auto.csv   rule predictions + which term triggered them
"""

import csv
import re
import sys
import unicodedata
from pathlib import Path

LABELS = Path("data/labels")

# Stems matched against lowercase, accent-free alt text + keywords.
# Kept fairly specific on purpose: words like "cozinha" also appear on appliance pages.
GROCERY_TERMS = [
    # fresh food
    "fruta", "legume", "fresco", "carne", "frango", "porco", "bovino", "peru", "hamburguer", "almondega",
    "peixe", "marisco", "salmao", "pescada", "bacalhau", "camarao", "atum", "choco", "polvo",
    "padaria", "pastelaria", "pao", "baguete", "croissant", "bolo",
    "queijo", "fiambre", "presunto", "charcutaria", "chourico", "enchido",
    "leite", "iogurte", "laticin", "manteiga", "nata", "ovo",
    "batata", "tomate", "cebola", "alho", "couve", "brocolo", "curgete", "abobora", "cenoura", "alface",
    "maca", "pera", "banana", "laranja", "roma", "diospiro", "anona", "uva", "kiwi", "limao",
    "noz", "amendoa", "fruto",
    # pantry, frozen, drinks
    "mercearia", "arroz", "massa", "farinha", "azeite", "oleo", "conserva", "cereais", "bolacha",
    "chocolate", "cafe", "cha", "acucar", "molho", "snack", "congelad", "pizza", "gelado",
    "bebida", "agua", "sumo", "refrigerante", "cerveja", "vinho",
    # household consumables and personal care
    "detergente", "amaciador", "lixivia", "limpeza", "papel higienico", "rolo de cozinha",
    "champo", "gel de banho", "pasta de dentes", "desodorizante", "fralda",
    # pet food (not pet toys)
    "racao", "comida para", "croquete", "patê",
]


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# Match stems only at the start of a word: "ovo" must not match "novo", "pera" not "espera".
TERMS = [(normalise(t), re.compile(r"\b" + re.escape(normalise(t)))) for t in GROCERY_TERMS]


def read_manifest(folder: Path) -> list[dict]:
    with open(folder / "manifest.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def predict(row: dict) -> tuple[int, str]:
    text = normalise(f"{row['alt_text']} {row['keywords']}")
    for term, pattern in TERMS:
        if pattern.search(text):
            return 1, term
    return 0, ""


def paths(folder: Path) -> tuple[Path, Path]:
    flyer_id = folder.name
    return LABELS / f"pages_{flyer_id}_gold.csv", LABELS / f"pages_{flyer_id}_auto.csv"


def cmd_template(folder: Path) -> None:
    gold, _ = paths(folder)
    if gold.exists():
        sys.exit(f"{gold} already exists; not overwriting your labels.")
    LABELS.mkdir(parents=True, exist_ok=True)
    with open(gold, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "file", "relevant", "note"])
        for row in read_manifest(folder):
            w.writerow([row["page"], row["file"], "", ""])
    print(f"Wrote {gold}. Fill 'relevant' with 1 or 0 by looking at each image, before running 'auto'.")


def cmd_auto(folder: Path) -> None:
    _, auto = paths(folder)
    LABELS.mkdir(parents=True, exist_ok=True)
    rows = read_manifest(folder)
    with open(auto, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "predicted", "matched_term", "alt_text"])
        for row in rows:
            pred, term = predict(row)
            w.writerow([row["page"], pred, term, row["alt_text"]])
    kept = sum(predict(r)[0] for r in rows)
    print(f"Wrote {auto}: keep {kept} of {len(rows)} pages, skip {len(rows) - kept}.")


def cmd_eval(folder: Path) -> None:
    gold_path, auto_path = paths(folder)
    with open(gold_path, encoding="utf-8") as f:
        gold = {r["page"]: r["relevant"].strip() for r in csv.DictReader(f)}
    missing = [p for p, v in gold.items() if v not in ("0", "1")]
    if missing:
        sys.exit(f"Pages without a 0/1 label in {gold_path}: {', '.join(missing)}")
    with open(auto_path, encoding="utf-8") as f:
        auto = {r["page"]: r for r in csv.DictReader(f)}

    tp = fp = fn = tn = 0
    for page, g in gold.items():
        p = auto[page]["predicted"]
        if g == "1" and p == "1": tp += 1
        elif g == "0" and p == "1": fp += 1
        elif g == "1" and p == "0":
            fn += 1
            print(f"MISSED relevant page {page}: {auto[page]['alt_text']}")
        else: tn += 1
    for page, g in gold.items():
        if g == "0" and auto[page]["predicted"] == "1":
            print(f"Kept non-relevant page {page} (term '{auto[page]['matched_term']}'): {auto[page]['alt_text']}")

    recall = tp / (tp + fn) if tp + fn else float("nan")
    precision = tp / (tp + fp) if tp + fp else float("nan")
    print(f"\npages={len(gold)}  relevant={tp + fn}  kept={tp + fp}  skipped={fn + tn}")
    print(f"recall={recall:.2f} (missed {fn})  precision={precision:.2f} (extra {fp})")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("template", "auto", "eval"):
        sys.exit(__doc__)
    folder = Path(sys.argv[2])
    {"template": cmd_template, "auto": cmd_auto, "eval": cmd_eval}[sys.argv[1]](folder)


if __name__ == "__main__":
    main()
