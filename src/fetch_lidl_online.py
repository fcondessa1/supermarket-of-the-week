"""This week's Lidl offers from lidl.pt product pages: shelf price, Lidl Plus price, dates, pack size,
unit price, and which products match my list items.

Usage:
    python src/fetch_lidl_online.py             # all products in Lidl's product sitemap
    python src/fetch_lidl_online.py --debug     # also save the first product's decoded data to scratch/

What lidl.pt has (checked 2026-10-22):
    - A product sitemap (https://www.lidl.pt/p/export/PT/pt/product_sitemap.xml.gz) with ~241 products:
      mostly this week's offers and seasonal items, NOT Lidl's full range. Products that are not on
      offer have no price anywhere on lidl.pt.
    - Product pages built with Nuxt: the data is one JSON array in <script id="__NUXT_DATA__"> where
      values are references to other positions ({"price": 84} = "see element 84").
      Terra Nostra Leite Pastagem Meio Gordo decoded to: lidlPlus price 0.79 "Com Lidl Plus",
      oldPrice/recommendedPrice 1.07, discountText "-26%", packaging "Cada emb. 1 L",
      startDate 2026-10-14T23:00Z, endDate 2026-10-25T22:59:59Z.

Output: data/prices/lidl/<YYYY-MM-DD>.csv (committed: price history), and matches for data/shopping/list.csv.
Cost: no LLM calls. One request per product, one per second (about 4-5 minutes).
"""

import csv
import gzip
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from continente_catalogue import candidates as match_candidates, normalise, tokens
from fetch_continente_online import drained_size_text, parse_size

SITEMAP = "https://www.lidl.pt/p/export/PT/pt/product_sitemap.xml.gz"
LIST = Path("data/shopping/list.csv")
OUT = Path("data/prices/lidl")
DEBUG_DIR = Path("scratch")
HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
FIELDS = ["date", "product_id", "name", "brand", "category", "packaging", "shelf_price", "old_price",
          "discount_text", "lidl_plus_price", "start_date", "end_date", "unit_price", "unit_price_total", "unit", "unit_price_basis",
          "weight_basis", "url", "note"]
LISBON = ZoneInfo("Europe/Lisbon")
WRAPPERS = {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}


# ---------- Nuxt payload ----------

def load_payload(page: str) -> list:
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(m.group(1)) if m else []


def resolve(data: list, idx, depth: int = 0, seen: frozenset = frozenset()):
    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(data) or idx in seen or depth > 14:
        return None
    seen = seen | {idx}
    v = data[idx]
    if isinstance(v, dict):
        return {k: resolve(data, i, depth + 1, seen) for k, i in v.items()}
    if isinstance(v, list):
        if v and isinstance(v[0], str) and v[0] in WRAPPERS:
            return resolve(data, v[1], depth + 1, seen) if len(v) > 1 else None
        if v and isinstance(v[0], str):          # Date, Set, Map... kept as-is
            return v
        return [resolve(data, i, depth + 1, seen) for i in v]
    return v


def product_object(data: list) -> dict:
    """The page's main product: the payload object with both erpNumber and keyfacts."""
    for i, v in enumerate(data):
        if isinstance(v, dict) and "erpNumber" in v and "keyfacts" in v:
            return resolve(data, i)
    return {}


def find_dicts(obj, pred, path=()):
    """All nested dicts satisfying pred, with their key path."""
    if isinstance(obj, dict):
        if pred(obj):
            yield path, obj
        for k, v in obj.items():
            yield from find_dicts(v, pred, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_dicts(v, pred, path + (i,))


# ---------- prices ----------

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def to_local_date(iso: str) -> str:
    """Lidl's dates are UTC: '2026-10-14T23:00Z' is 15 Oct 00:00 in Lisbon (summer time, UTC+1)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return dt.astimezone(LISBON).date().isoformat()


def packaging_size(text: str) -> tuple[tuple[float, str] | None, str]:
    """'Cada emb. 1 L' -> ((1.0, 'l'), ''); '120 g (peso escorrido 84 g)' -> ((0.084, 'kg'), 'drained');
    'kg' / 'Preço por kg' -> ((1.0, 'kg'), 'per kg')."""
    # Prefixes seen live on 2026-10-27: "Cada emb.", "Emb.", "Pack 4x110 g", "Cada pack 4x85 g".
    t = re.sub(r"^\s*(cada\s+)?(emb\.?|embalagem|pack|unid\.?)\s*", "", text or "", flags=re.I)
    drained = drained_size_text(t)
    if drained:
        return parse_size(drained), "drained"
    size = parse_size(t)
    if size:
        return size, ""
    if re.search(r"\bkg\b", t, re.I):
        return (1.0, "kg"), "per kg"
    return None, ""


def parse_product(page: str, url: str) -> dict:
    data = load_payload(page)
    p = product_object(data)
    row = {k: "" for k in FIELDS} | {"url": url}
    if not p:
        row["note"] = "no product data on page" if data else "no __NUXT_DATA__ block"
        return row
    kf = p.get("keyfacts") or {}
    info = (p.get("info") or {}).get("brand") or {}
    row |= {"product_id": str(p.get("erpNumber") or p.get("itemId") or ""),
            "name": kf.get("fullTitle") or kf.get("title") or "",
            "brand": info.get("name") or "",
            "category": (kf.get("wonCategoryPrimary") or "").replace("Mundos de necessidade/", "")}

    shelf = p.get("price") if isinstance(p.get("price"), dict) else {}
    plus_list = p.get("lidlPlus") or []
    plus = (plus_list[0] or {}).get("price") if plus_list and isinstance(plus_list[0], dict) else {}
    plus = plus if isinstance(plus, dict) else {}
    # Dates live on the region price objects (currentLidlPlusPrice / current price); take the first with an endDate.
    dated = [d for _, d in find_dicts(p, lambda d: "endDate" in d and _num(d.get("price")) is not None)]

    shelf_price = _num(shelf.get("price"))
    plus_price = _num(plus.get("price"))
    old = _num(shelf.get("oldPrice")) or _num(plus.get("oldPrice")) or _num((plus.get("discount") or {}).get("deletedPrice"))
    notes = []
    if shelf_price is None and plus_price is not None and old is not None:
        shelf_price = old
        notes.append("no shelf price on page; shelf price taken as the old price (price without Lidl Plus)")
    packaging = (shelf.get("packaging") or plus.get("packaging") or {}).get("text", "")
    row |= {"packaging": packaging,
            "shelf_price": _f(shelf_price), "old_price": _f(old),
            "discount_text": ((shelf.get("discount") or plus.get("discount") or {}).get("discountText") or ""),
            "lidl_plus_price": _f(plus_price),
            "start_date": to_local_date(dated[0].get("startDate", "")) if dated else "",
            "end_date": to_local_date(dated[0].get("endDate", "")) if dated else ""}

    if plus_price is not None and shelf_price is None:
        notes.append("Lidl Plus price only: the price without the app is not shown")
    best = plus_price if plus_price is not None else shelf_price
    size, basis = packaging_size(packaging)
    if best is not None and size:
        qty, unit = size
        row |= {"unit_price": f"{best / qty:.3f}", "unit": unit,
                "unit_price_basis": "lidl_plus" if plus_price is not None else "shelf",
                "weight_basis": "drained" if basis == "drained" else ""}
        total = parse_size(re.sub(r"^\s*(cada\s+)?(emb\.?|embalagem|pack|unid\.?)\s*", "", packaging, flags=re.I))
        row["unit_price_total"] = (f"{best / total[0]:.3f}" if basis == "drained" and total and total[1] == unit
                                   else row["unit_price"])
        if basis == "per kg":
            notes.append("price assumed per kg (sold by weight)")
    elif best is None:
        notes.append("no price on page (product not on offer?)")
    elif not re.sub(r"^\s*(cada\s+)?(emb\.?|embalagem|pack|unid\.?)\s*", "", packaging, flags=re.I).strip():
        notes.append("no pack size given")          # e.g. "Cada emb." on plants and non-food
    else:
        notes.append(f"packaging not understood: {packaging!r}")
    row["note"] = "; ".join(notes)
    return row


def _f(v) -> str:
    return "" if v is None else f"{v:.2f}"


# ---------- run ----------

def product_urls() -> list[str]:
    raw = requests.get(SITEMAP, headers=HEADERS, timeout=60).content
    try:
        xml = gzip.decompress(raw).decode("utf-8")
    except OSError:
        xml = raw.decode("utf-8")
    return list(dict.fromkeys(u.strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml) if "/p/" in u))


def match_list(rows: list[dict]) -> None:
    """Show which of this week's Lidl products match my list items (same word rules as Continente).
    Not reviewed: Lidl's products change every week, so these are suggestions, shown with their category."""
    if not LIST.exists():
        return
    with open(LIST, encoding="utf-8") as f:
        items = [r for r in csv.DictReader(f) if (r.get("item") or "").strip()]
    catalogue = [{"product_id": r["product_id"], "name": r["name"], "_tokens": tokens(r["name"]),
                  "_norm": normalise(r["name"]), "row": r} for r in rows if r["name"]]
    print("\nMatches for my list (unreviewed):")
    for it in items:
        found = match_candidates(catalogue, it["item"], it.get("exclude") or "")
        if not found:
            print(f"  {it['item']}: no Lidl offer this week")
            continue
        for c in found:
            r = c["row"]
            plus = f" (Lidl Plus {r['lidl_plus_price']}, else {r['shelf_price']})" if r["lidl_plus_price"] else ""
            up = f"{float(r['unit_price']):.2f} €/{r['unit']}" if r["unit_price"] else "no unit price"
            print(f"  {it['item']}: {r['name'][:50]} {r['packaging']} -> {up}{plus} {r['start_date']}..{r['end_date']}")


def main() -> None:
    debug = "--debug" in sys.argv
    urls = product_urls()
    print(f"{len(urls)} products in Lidl's product sitemap; fetching (about {len(urls) * (DELAY_S + 0.5) / 60:.0f} min)...")
    today = date.today().isoformat()
    rows = []
    for i, url in enumerate(urls, 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            row = parse_product(resp.text, url)
            if debug and i == 1:
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "lidl_first_product.json").write_text(
                    json.dumps(product_object(load_payload(resp.text)), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except requests.RequestException as e:
            row = {k: "" for k in FIELDS} | {"url": url, "note": f"request failed: {e}"}
        rows.append(row | {"date": today})
        if i % 50 == 0:
            print(f"  {i}/{len(urls)}")
        time.sleep(DELAY_S)

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"{today}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    priced = [r for r in rows if r["shelf_price"] or r["lidl_plus_price"]]
    food = [r for r in rows if r["category"].lower().startswith("alimentos")]
    print(f"\n{len(rows)} products -> {out_path}")
    print(f"with a price: {len(priced)}; with a Lidl Plus price: {sum(bool(r['lidl_plus_price']) for r in rows)}; "
          f"with a unit price: {sum(bool(r['unit_price']) for r in rows)}; food/near-food category: {len(food)}")
    notes = {}
    for r in rows:
        for n in filter(None, r["note"].split("; ")):
            key = n.split(":")[0]
            notes[key] = notes.get(key, 0) + 1
    if notes:
        print("notes:", notes)
    match_list(rows)


if __name__ == "__main__":
    main()
