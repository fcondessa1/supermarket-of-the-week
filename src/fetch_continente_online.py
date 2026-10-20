"""This week's Continente Online prices for every product that counts as one of my list items,
converted to price per kg / L / unit, and the cheapest options per item.

Usage:
    python src/fetch_continente_online.py

Input:  data/shopping/continente_products.csv (accepted products, from `continente_catalogue.py review`)
Output: data/prices/continente/<YYYY-MM-DD>.csv, one row per product, and a summary per item.

Where each number comes from (checked on 2026-10-20 on product 4676679, Pescanova Supremas):
    price, price_valid_until   JSON-LD Product block: offers.price 8.99, offers.priceValidUntil 2026-10-26
    pre_discount_price,        analytics data layer, the entry whose item_id is this product:
      discount, sold_by,         pre_discount_price 11.28, discount 2.29, measurement_unit "un" ("kg" = sold by weight),
      category                   item_category2/3 "Peixaria / Bacalhau Congelado"
    size_text                  meta description: "... emb. 400 gr ..."
    page_unit_price            Continente's own figure printed on the page: "22,47€/kg"

    unit_price is computed by this script (price / pack size). page_unit_price is kept as an
    independent check: `check` says whether the two agree, which is the normalisation eval.

Caveat: ONLINE prices. Continente's leaflet warns that store prices and promotions may differ.
Price files hold only public prices and are committed: they become the price history.
Cost: no LLM calls. One request per accepted product, one per second.
"""

import csv
import html
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

PRODUCTS = Path("data/shopping/continente_products.csv")
OUT = Path("data/prices/continente")
HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
FIELDS = ["date", "item", "product_id", "name", "brand", "category", "price", "pre_discount_price",
          "discount", "on_promotion", "price_valid_until", "sold_by", "size_text", "pack_quantity",
          "unit_price", "unit", "page_unit_price", "check", "url", "note"]


# ---------- page parsing ----------

def json_ld_product(page: str) -> dict:
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "Product":
                return obj
    return {}


def enclosing_object(text: str, pos: int, limit: int = 5000) -> str:
    """The innermost {...} around position pos."""
    depth, start = 0, None
    for i in range(pos, max(-1, pos - limit), -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return ""
    depth = 0
    for j in range(start, min(len(text), start + 2 * limit)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start: j + 1]
    return ""


def data_layer_entry(page: str, product_id: str) -> dict:
    """The analytics entry for THIS product (pages also carry entries for recommended products)."""
    text = html.unescape(page)
    for m in re.finditer(r'"pre_discount_price"\s*:', text):
        entry = enclosing_object(text, m.start())
        try:
            obj = json.loads(entry)
        except json.JSONDecodeError:
            continue
        if str(obj.get("item_id")) == product_id:
            return obj
    return {}


def size_text_from_meta(page: str) -> str:
    m = re.search(r'<meta name="description" content="([^"]*)"', page)
    if not m:
        return ""
    s = re.search(r"\bemb\.\s*(.+?)(?:\s+no Continente|$)", html.unescape(m.group(1)), re.I)
    return s.group(1).strip() if s else ""


def page_unit_prices(page: str) -> list[tuple[float, str]]:
    """All 'x,xx€/kg' style figures on the page, as (value, unit) with unit in kg/l/unit."""
    text = html.unescape(page)
    out = []
    for v, u in re.findall(r"(\d+[.,]\d{2})\s*€\s*/\s*(kg|lt|l|un)\b", text, re.I):
        unit = {"lt": "l", "l": "l", "kg": "kg", "un": "unit"}[u.lower()]
        out.append((float(v.replace(",", ".")), unit))
    return list(dict.fromkeys(out))


# ---------- pack size ----------

UNIT_FACTORS = {  # -> (base unit, factor)
    "kg": ("kg", 1.0), "g": ("kg", 0.001), "gr": ("kg", 0.001), "grs": ("kg", 0.001),
    "l": ("l", 1.0), "lt": ("l", 1.0), "lts": ("l", 1.0), "litro": ("l", 1.0), "litros": ("l", 1.0),
    "cl": ("l", 0.01), "ml": ("l", 0.001),
    "un": ("unit", 1.0), "unid": ("unit", 1.0), "unidades": ("unit", 1.0),
}


def parse_size(text: str) -> tuple[float, str] | None:
    """'400 gr' -> (0.4, 'kg'); '6 x 1 lt' -> (6.0, 'l'); '1,5 lt' -> (1.5, 'l'); '12 un' -> (12, 'unit').
    Returns None when the text isn't a recognisable pack size."""
    t = text.lower().replace(",", ".")
    m = re.match(r"\s*(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*([a-z]+)\b", t)
    if not m or m.group(3) not in UNIT_FACTORS:
        return None
    count = int(m.group(1)) if m.group(1) else 1
    base, factor = UNIT_FACTORS[m.group(3)]
    qty = count * float(m.group(2)) * factor
    return (round(qty, 6), base) if qty > 0 else None


# ---------- one product ----------

def parse_product_page(page: str, product_id: str) -> dict:
    ld = json_ld_product(page)
    offers = ld.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    layer = data_layer_entry(page, product_id)
    notes = []
    if not ld:
        notes.append("no JSON-LD Product")
    if not layer:
        notes.append("no data-layer entry for this product")

    price = _num(offers.get("price")) or _num(layer.get("price"))
    pre = _num(layer.get("pre_discount_price"))
    disc = _num(layer.get("discount")) or 0.0
    sold_by = (layer.get("measurement_unit") or "").lower()
    size_text = size_text_from_meta(page)

    unit_price, unit, pack_qty = None, "", None
    if price is not None:
        if sold_by == "kg":                              # sold by weight: the price is already per kg
            unit_price, unit = price, "kg"
        else:
            size = parse_size(size_text) if size_text else None
            if size:
                pack_qty, unit = size
                unit_price = price / pack_qty
            else:
                notes.append(f"pack size not understood: {size_text!r}" if size_text else "no pack size on page")

    page_units = page_unit_prices(page)
    check, page_up = "", None
    if unit:
        same_unit = [v for v, u in page_units if u == unit]
        if same_unit and unit_price is not None:
            page_up = min(same_unit, key=lambda v: abs(v - unit_price))
            ok = abs(page_up - unit_price) <= max(0.02, 0.01 * unit_price)
            check = "ok" if ok else "differs"
    elif len({u for _, u in page_units if u in ("kg", "l")}) == 1:   # size unknown: fall back on the page's figure
        page_up, unit = next((v, u) for v, u in page_units if u in ("kg", "l"))
        unit_price, check = page_up, "page only"
        notes.append("unit price taken from the page")

    if price is not None and pre and disc and abs(pre - disc - price) > 0.011:
        notes.append(f"price {price} != pre_discount_price - discount ({pre - disc:.2f})")

    return {
        "product_id": product_id,
        "name": ld.get("name") or layer.get("item_name", ""),
        "brand": (ld.get("brand") or {}).get("name", "") if isinstance(ld.get("brand"), dict) else layer.get("item_brand", ""),
        "category": " / ".join(x for x in (layer.get("item_category2"), layer.get("item_category3")) if x),
        "price": _fmt(price), "pre_discount_price": _fmt(pre), "discount": _fmt(disc) if disc else "",
        "on_promotion": "yes" if disc > 0 else "no",
        "price_valid_until": offers.get("priceValidUntil", ""),
        "sold_by": sold_by, "size_text": size_text,
        "pack_quantity": f"{pack_qty:g}" if pack_qty else "",
        "unit_price": _fmt(unit_price, 3), "unit": unit, "page_unit_price": _fmt(page_up),
        "check": check, "note": "; ".join(notes),
    }


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "", {}) else None
    except (TypeError, ValueError):
        return None


def _fmt(v: float | None, decimals: int = 2) -> str:
    return "" if v is None else f"{v:.{decimals}f}"


# ---------- run ----------

def main() -> None:
    if not PRODUCTS.exists():
        sys.exit(f"{PRODUCTS} not found. Run: python src/continente_catalogue.py review")
    with open(PRODUCTS, encoding="utf-8") as f:
        accepted = [r for r in csv.DictReader(f) if r["decision"] == "accepted"]
    if not accepted:
        sys.exit("No accepted products yet. Run: python src/continente_catalogue.py review")

    today = date.today().isoformat()
    print(f"Fetching {len(accepted)} product pages (about {len(accepted) * (DELAY_S + 0.5) / 60:.0f} min)...")
    rows = []
    for i, p in enumerate(accepted, 1):
        try:
            resp = requests.get(p["url"], headers=HEADERS, timeout=60)
            resp.raise_for_status()
            row = parse_product_page(resp.text, p["product_id"])
        except requests.RequestException as e:
            row = {k: "" for k in FIELDS} | {"product_id": p["product_id"], "name": p["name"], "note": f"request failed: {e}"}
        rows.append(row | {"date": today, "item": p["item"], "url": p["url"]})
        if i % 25 == 0:
            print(f"  {i}/{len(accepted)}")
        time.sleep(DELAY_S)

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"{today}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    by_item = defaultdict(list)
    for r in rows:
        by_item[r["item"]].append(r)
    for item, rs in by_item.items():
        priced = sorted((r for r in rs if r["unit_price"]), key=lambda r: float(r["unit_price"]))
        print(f"\n== {item}: {len(priced)} of {len(rs)} products priced per {priced[0]['unit'] if priced else '?'}")
        for r in priced[:3]:
            promo = f"  PROMO was {r['pre_discount_price']} until {r['price_valid_until']}" if r["on_promotion"] == "yes" else ""
            print(f"   {float(r['unit_price']):7.2f} €/{r['unit']:<4} {r['price']:>6} € {r['name'][:55]}{promo}")
        units = {r["unit"] for r in priced}
        if len(units) > 1:
            print(f"   WARNING: mixed units {sorted(units)}: some products are compared per kg, others per L or unit")
    problems = [r for r in rows if r["note"] or r["check"] == "differs"]
    print(f"\n{len(rows)} products -> {out_path}")
    print(f"{sum(r['check'] == 'ok' for r in rows)} unit prices agree with Continente's own figure, "
          f"{sum(r['check'] == 'differs' for r in rows)} differ, {len(problems)} rows with notes (see the CSV)")


if __name__ == "__main__":
    main()
