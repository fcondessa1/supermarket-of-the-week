"""One-off experiment: can a model read a Lidl leaflet page as well as I do?

Usage:
    python src/leaflet_extract.py estimate                  # pages found, image tokens, cost per model; no API calls
    python src/leaflet_extract.py run --model haiku         # read every labelled page (answers saved; never re-asked)
    python src/leaflet_extract.py score --model haiku       # compare with my labels, and with lidl.pt

Pages: every hand-labelled page in data/labels/offers/lidl_<flyer>_p<NN>.csv (5 pages picked at random among the
pages the page filter kept, labelled and committed before any model run). Images from data/raw/lidl/<flyer>/.

The model gets the page image and the same labelling guide I used (data/labels/OFFERS_GUIDE.md), and returns
one row per offer in the label columns. Its answers go to data/llm/leaflet/<model>/<label file name>.csv.

Scoring, per page and in total:
    offers found      my offers that the model also lists (matched by name, with the price as a tiebreak)
    invented offers   offers the model lists that I don't have
    fields            for matched offers: price, price_per, regular price, unit price, condition, size, brand
    lidl.pt check     model prices vs this week's lidl.pt prices, for products whose names match closely
                      (an independent source, since my labels and the model could share a misreading)

Cost: images are sent as they are (1200 px wide pages: about 3,100 visual tokens each, within the 4,784-token
limit of the high-resolution tier that Claude Haiku 5.5 uses, docs checked 2026-11-11). `estimate` computes
it per page; every run prints the measured cost from the API's token counts.
"""

import base64
import csv
import json
import math
import os
import re
import struct
import sys
import time
import unicodedata
from pathlib import Path

LABELS = Path("data/labels/offers")
GUIDE = Path("data/labels/OFFERS_GUIDE.md")
RAW = Path("data/raw/lidl")
OUT = Path("data/llm/leaflet")
MODELS = {  # name -> (model id, $ per million input tokens, $ per million output tokens)
    "haiku": ("claude-haiku-5-5", 0.10, 0.50),
    "sonnet": ("claude-sonnet-5-5", 2.00, 10.00),
}
MAX_EDGE, MAX_TOKENS = 2576, 4784            # high-resolution image tier
COLUMNS = ["offer", "name", "brand", "size", "price", "price_per", "regular_price",
           "unit_price", "unit_price_per", "condition", "discount_text", "notes"]

SCHEMA = {
    "type": "object",
    "properties": {"offers": {"type": "array", "items": {
        "type": "object",
        "properties": {c: {"type": "string"} for c in COLUMNS},
        "required": COLUMNS,
        "additionalProperties": False,
    }}},
    "required": ["offers"],
    "additionalProperties": False,
}


# ---------- pages and images ----------

def labelled_pages() -> list[tuple[Path, Path]]:
    """(label file, page image) for every Lidl label file that has a matching downloaded image."""
    out = []
    for label in sorted(LABELS.glob("lidl_*_p*.csv")):
        m = re.match(r"lidl_([0-9a-f]+)_p(\d+)\.csv$", label.name)
        folders = [f for f in RAW.glob(f"{m.group(1)}*") if f.is_dir()] if m else []
        image = folders[0] / f"page-{int(m.group(2)):02d}.jpg" if folders else None
        if image and image.exists():
            out.append((label, image))
        else:
            print(f"skip {label.name}: no image found under {RAW}/{m.group(1) if m else '?'}*")
    return out


def jpeg_size(path: Path) -> tuple[int, int]:
    """(width, height) from the JPEG header, without an image library."""
    data = path.read_bytes()
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError(f"{path}: not a JPEG I can read")


def image_tokens(w: int, h: int) -> int:
    """Visual tokens after the API's own downscaling (one token per 28x28 patch)."""
    scale = 1.0
    while True:
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        tokens = math.ceil(sw / 28) * math.ceil(sh / 28)
        if max(sw, sh) <= MAX_EDGE and tokens <= MAX_TOKENS:
            return tokens
        scale *= 0.98


# ---------- model ----------

def prompt_text() -> str:
    return (GUIDE.read_text(encoding="utf-8") +
            "\n\nThe image is one page of a Lidl Portugal leaflet. List every offer on it, following the guide "
            "above exactly, one object per offer with every column (empty string when a column doesn't apply). "
            "Copy names as printed. Use a dot as the decimal separator.")


def ask(client, model_id: str, image: Path) -> tuple[list[dict], dict]:
    resp = client.messages.create(
        model=model_id, max_tokens=8000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image.read_bytes()).decode()}},
            {"type": "text", "text": prompt_text()},
        ]}],
        extra_body={"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens, "stop": resp.stop_reason}
    return json.loads(text)["offers"], usage


# ---------- commands ----------

def cmd_estimate() -> None:
    pages = labelled_pages()
    if not pages:
        sys.exit(f"No labelled Lidl pages with images found in {LABELS}")
    prompt_tokens = len(prompt_text()) / 3.5            # rough: text tokens of the guide + instruction
    total_img = 0
    for label, image in pages:
        w, h = jpeg_size(image)
        t = image_tokens(w, h)
        total_img += t
        with open(label, encoding="utf-8") as f:
            n = sum(1 for r in csv.DictReader(f) if (r.get("name") or "").strip())
        print(f"  {label.name}: {w}x{h} px -> {t:,} image tokens; {n} offers in my labels")
    n_offers = sum(sum(1 for r in csv.DictReader(open(l, encoding="utf-8")) if (r.get("name") or "").strip())
                   for l, _ in pages)
    est_in = total_img + len(pages) * prompt_tokens
    est_out = n_offers * 90 + len(pages) * 50           # ~90 output tokens per offer row
    print(f"\n{len(pages)} pages: ~{est_in:,.0f} input + ~{est_out:,.0f} output tokens")
    for name, (mid, pin, pout) in MODELS.items():
        print(f"  {name:6} ({mid}): ~${est_in / 1e6 * pin + est_out / 1e6 * pout:.3f}")


def cmd_run(model: str) -> None:
    model_id, pin, pout = MODELS[model]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set.")
    import anthropic
    client = anthropic.Anthropic()
    out_dir = OUT / model
    out_dir.mkdir(parents=True, exist_ok=True)
    used_in = used_out = 0
    for label, image in labelled_pages():
        path = out_dir / label.name
        if path.exists():
            print(f"  {label.name}: already read by {model}, not asked again")
            continue
        t0 = time.time()
        offers, usage = ask(client, model_id, image)
        used_in += usage["input"]
        used_out += usage["output"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for o in offers:
                w.writerow({c: (o.get(c) or "").strip() for c in COLUMNS})
        note = "" if usage["stop"] == "end_turn" else f"  (stopped: {usage['stop']})"
        print(f"  {label.name}: {len(offers)} offers, {usage['input']:,} in / {usage['output']:,} out tokens, "
              f"{time.time() - t0:.0f} s{note}")
    print(f"measured cost: {used_in:,} input + {used_out:,} output tokens = ${used_in / 1e6 * pin + used_out / 1e6 * pout:.4f}")


# ---------- scoring ----------

def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", norm(text)) if len(w) > 1}


def similarity(a: dict, b: dict) -> float:
    wa, wb = words(a["name"] + " " + a["brand"]), words(b["name"] + " " + b["brand"])
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def num(v: str) -> float | None:
    try:
        return float((v or "").replace(",", "."))
    except ValueError:
        return None


def same_number(a: str, b: str) -> bool:
    x, y = num(a), num(b)
    return (x is None and y is None) or (x is not None and y is not None and abs(x - y) < 0.006)


def same_size(a: str, b: str) -> bool:
    """Same size, ignoring how it's introduced: "Emb. 275 g", "Cada emb. 230 g", "Pack 8x155 g" and "Vaso: 11 cm"
    equal "275 g", "230 g", "8 x 155 g" and "vaso 11 cm". (v1 counted those as wrong: 13 of Haiku's 18 size
    errors on 2026-11-11, and the whole Haiku-Sonnet difference.)"""
    def f(s: str) -> str:
        s = norm(s).replace(",", ".")
        s = re.sub(r"\b(cada\s+emb|emb|pack|vaso|embalagem)\b\.?:?", " ", s)
        return re.sub(r"\s+", "", s).replace("gr", "g").replace("lt", "l")
    return f(a) == f(b)


FIELDS = {
    "price": lambda a, b: same_number(a["price"], b["price"]),
    "price_per": lambda a, b: a["price_per"].strip().lower() == b["price_per"].strip().lower(),
    "regular_price": lambda a, b: same_number(a["regular_price"], b["regular_price"]),
    "unit_price": lambda a, b: same_number(a["unit_price"], b["unit_price"]),
    "condition": lambda a, b: a["condition"].strip().lower() == b["condition"].strip().lower(),
    "size": lambda a, b: same_size(a["size"], b["size"]),
    "brand": lambda a, b: words(a["brand"]) == words(b["brand"]),
}


def read_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [{c: (r.get(c) or "").strip() for c in COLUMNS} for r in csv.DictReader(f) if (r.get("name") or "").strip()]


def match(mine: list[dict], model: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy one-to-one matching: best name similarity first; an equal price adds a little, so two products
    with similar names (two Ferrero Rocher packs) pair up by price."""
    pairs = sorted(((similarity(a, b) + (0.15 if same_number(a["price"], b["price"]) else 0), i, j)
                    for i, a in enumerate(mine) for j, b in enumerate(model)), reverse=True)
    used_i, used_j, out = set(), set(), []
    for s, i, j in pairs:
        if s >= 0.35 and i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
            out.append((mine[i], model[j]))
    return out


def lidl_check(model_rows: list[dict]) -> None:
    files = sorted(Path("data/prices/lidl").glob("*.csv"))
    if not files:
        return
    with open(files[-1], encoding="utf-8") as f:
        site = [r for r in csv.DictReader(f) if r.get("name")]
    checked = agree = 0
    misses = []
    for o in model_rows:
        # every close name counts, not only the closest: a page can hold two packs of one product (Ferrero
        # Rocher 300 g at 7.99 and 375 g at 8.19), and v1 compared one with the other
        sims = [(similarity(o, {"name": r["name"], "brand": r.get("brand", "")}), r) for r in site]
        top = max((sm for sm, _ in sims), default=0)
        if top < 0.6:
            continue
        close = [r for sm, r in sims if sm >= top - 0.05]
        best = close[0]
        site_prices = [num(r.get(k, "")) for r in close for k in ("lidl_plus_price", "shelf_price")]
        checked += 1
        if any(p is not None and abs(p - (num(o["price"]) or -1)) < 0.006 for p in site_prices):
            agree += 1
        else:
            misses.append(f"{o['name'][:40]} {o['price']} vs lidl.pt {best['name'][:40]} "
                          f"{best.get('lidl_plus_price') or best.get('shelf_price')}")
    print(f"\n   lidl.pt check ({files[-1].stem}): {checked} model offers found on lidl.pt, {agree} at the same price")
    for m in misses[:8]:
        print(f"     differs: {m}")


def cmd_score(model: str) -> None:
    out_dir = OUT / model
    totals = {"mine": 0, "model": 0, "found": 0} | {k: [0, 0] for k in FIELDS}
    report = []
    all_model = []
    for label, _ in labelled_pages():
        pred = out_dir / label.name
        if not pred.exists():
            print(f"  {label.name}: not read by {model} yet (run first)")
            continue
        mine, theirs = read_rows(label), read_rows(pred)
        all_model += theirs
        pairs = match(mine, theirs)
        totals["mine"] += len(mine)
        totals["model"] += len(theirs)
        totals["found"] += len(pairs)
        for a, b in pairs:
            for k, same in FIELDS.items():
                ok = same(a, b)
                totals[k][0] += ok
                totals[k][1] += 1
                if not ok:
                    report.append({"page": label.name, "what": k, "product": a["name"], "mine": a[k], "model": b[k]})
        found_mine = {id(a) for a, _ in pairs}
        found_theirs = {id(b) for _, b in pairs}
        report += [{"page": label.name, "what": "missed", "product": a["name"], "mine": a["price"], "model": ""}
                   for a in mine if id(a) not in found_mine]
        report += [{"page": label.name, "what": "invented", "product": b["name"], "mine": "", "model": b["price"]}
                   for b in theirs if id(b) not in found_theirs]
        print(f"  {label.name}: mine {len(mine)}, model {len(theirs)}, matched {len(pairs)}")

    if not totals["mine"]:
        return
    t = totals
    print(f"\n== {model} ({MODELS[model][0]}) on {t['mine']} offers I labelled")
    print(f"   offers found:    {t['found']} of {t['mine']} ({t['found'] / t['mine']:.0%})")
    print(f"   invented offers: {t['model'] - t['found']} of {t['model']} the model listed")
    print("   fields right, on the matched offers:")
    for k in FIELDS:
        ok, n = t[k]
        print(f"     {k:14} {ok}/{n}" + (f" ({ok / n:.0%})" if n else ""))
    path = OUT / f"score_{model}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page", "what", "product", "mine", "model"])
        w.writeheader()
        w.writerows(report)
    print(f"   every difference: {path}")
    lidl_check(all_model)


def main() -> None:
    args = sys.argv[1:]
    model = args[args.index("--model") + 1] if "--model" in args else "haiku"
    if model not in MODELS:
        sys.exit(f"--model must be one of {', '.join(MODELS)}")
    if args[:1] == ["estimate"]:
        cmd_estimate()
    elif args[:1] == ["run"]:
        cmd_run(model)
    elif args[:1] == ["score"]:
        cmd_score(model)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
