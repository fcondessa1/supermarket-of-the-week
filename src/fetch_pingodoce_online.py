"""This week's Pingo Doce online prices for every product that counts as one of my list items.

Usage:
    python src/fetch_pingodoce_online.py               # add --recheck about once a month
    python src/fetch_pingodoce_online.py --test FILE   # parse a saved product page, no requests

Input:  data/shopping/pingodoce_products.csv (accepted products, from `python src/catalogue.py pingodoce review`)
Output: data/prices/pingodoce/<YYYY-MM-DD>.csv, same columns as Continente's price files.

The run itself (which products, redirects, checks, summary) is Continente's: both shops are built on
Salesforce Commerce Cloud. Only reading a product page differs. Where each number comes from
(checked on 2026-11-03 on product 48150, Leite UHT Meio Gordo Pingo Doce):
    price             analytics entry whose item_id is this product: "price": 0.85
                      (also <span class="value" content="0.85"> in the price block)
    discount, brand,  same entry: "discount": 0, "item_brand": "Pingo Doce",
      category          "item_category2": "Leite e Bebidas Vegetais", "item_category": "Leite Meio Gordo e Gordo"
    size_text,        <... class="product-unit-measure">1 L | 0,85 €/L
      page_unit_price   pack size before the "|", Pingo Doce's own unit price after it
    JSON-LD Product   name and sku only: "offers" is null, so no price and no promotion end date.

Not seen yet, so handled defensively: a promoted product. The price before the discount is taken from a
struck-through price (class "strike-through" / "list") if the page has one, otherwise price + discount.

Caveat: ONLINE prices, which may differ from the shop. Cost: no LLM calls. One request per product, one per second.
"""

import html
import re
import sys

from fetch_continente_online import (_fmt, _num, data_layer_entry, drained_size_text, json_ld_product, main as run,
                                     page_unit_prices, parse_size)


def unit_measure(page: str) -> tuple[str, str]:
    """'1 L | 0,85 €/L' -> ('1 L', '0,85 €/L'). The first one on the page is the product's own
    (recommended products come later)."""
    m = re.search(r'class="product-unit-measure"[^>]*>(.*?)<', page, re.S)
    if not m:
        return "", ""
    text = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    size, _, unit_price = text.partition("|")
    return size.strip(), unit_price.strip()


def struck_price(page: str) -> float | None:
    """The price before a promotion, as shown struck through next to the sales price (the platform's
    usual markup: <span class="strike-through list"><span class="value" content="1.09">)."""
    m = re.search(r'(?:class="[^"]*\bstrike-through\b[^"]*"|<del\b)[^>]*>.{0,300}?content="(\d+(?:\.\d+)?)"',
                  page, re.S)
    return _num(m.group(1)) if m else None


def parse_product_page(page: str, product_id: str) -> dict:
    ld = json_ld_product(page)
    layer = data_layer_entry(page, product_id)
    notes = []
    if not layer:
        notes.append("no analytics entry for this product")

    # 2026-11-03, first live run: most products had "price": 0 in the analytics entry, which v1 took as a
    # real price (everything at Pingo Doce for 0.00 €). Zero means no price: try the price block, else none.
    price = _num(layer.get("price"))
    if not price:
        m = re.search(r'class="sales"[^>]*>.*?content="(\d+(?:\.\d+)?)"', page, re.S)
        block = _num(m.group(1)) if m else None
        if block:
            price = block
            notes.append("price from the price block (analytics price missing or 0)")
        else:
            price = None
            notes.append("price 0 on page (not sold online, out of stock, or needs a postcode?)")
    disc = _num(layer.get("discount")) or 0.0
    pre = struck_price(page)
    if pre is not None and price is not None and pre <= price:
        pre = None                                   # not a real 'before' price
    if pre is None and disc > 0 and price is not None:
        pre = price + disc
        notes.append("price before discount computed as price + discount")
    on_promo = pre is not None and price is not None and pre > price

    size_text, page_unit_text = unit_measure(page)
    drained = drained_size_text(size_text) or drained_size_text(html.unescape(ld.get("description", "") or ""))
    weight_basis = "drained" if drained else ""
    unit_price, unit, pack_qty = None, "", None
    size = parse_size(drained or size_text) if (drained or size_text) else None
    if size and size[1] == "unit" and not float(size[0]).is_integer():
        # 2026-11-03: Pingo Doce writes some weights as counts: rice "1.5 Un" (1.5 kg), tuna "0.084 Un" (84 g).
        # A fractional count isn't a count, and guessing kg would be wrong for products sold by the litre.
        notes.append(f"pack size {size_text!r} is a fractional count (a weight written as units?): not used")
        size = None
    # Sold by weight: the price is per kg and the size is only an average. The price block says so just before
    # the "Adicionar" button: "6,59 €/Kg 1 Un 0,6 kg" (Peito de Frango Embalado, with "Peso aproximado da
    # unidade" further down) or "2,49 €/Kg 1 Un 0,37 kg" (Batata Doce, without it). Seen 2026-11-06; v1 divided
    # the per-kg price by the weight again (sweet potato 6.73 €/kg). Only the block before the first
    # "Adicionar" is read, so recommended products further down can't trigger it.
    page_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    add = page_text.lower().find("adicionar")
    block = page_text[max(0, add - 300):add] if add >= 0 else ""
    # (a 1 kg pack shows "1 Kg | 1,89 €/Kg" there too: same number either way, so it isn't called 'by weight')
    by_weight = price is not None and (not size or (size[1] == "kg" and size[0] != 1)) and any(
        abs(float(f"{a}.{b}") - price) < 0.006
        for a, b in re.findall(r"(\d+),(\d{2})\s*€\s*/\s*kg", block, re.I))
    if by_weight:
        notes.append(f"sold by weight: {price:.2f} €/kg; average pack {size_text or 'unknown'}")
    if price is not None:
        if by_weight:
            unit_price, unit = price, "kg"
        elif size:
            pack_qty, unit = size
            unit_price = price / pack_qty
        elif re.fullmatch(r"(1\s*)?kg", size_text.strip(), re.I):
            unit_price, unit = price, "kg"           # sold by weight: price per kg
        elif not any(n.startswith("pack size") for n in notes):
            notes.append(f"pack size not understood: {size_text!r}" if size_text else "no pack size on page")

    page_units = page_unit_prices(page_unit_text)
    check, page_up = "", None
    if by_weight:
        page_up, check = price, "per kg (page)"
    elif unit and unit_price is not None:
        same = [v for v, u in page_units if u == unit]
        if same:
            page_up = same[0]
            check = "ok" if abs(page_up - unit_price) <= max(0.02, 0.01 * unit_price) else "differs"
            if check == "differs" and unit in ("kg", "l"):
                notes.append(f"page €/{unit} implies {price / page_up * 1000:.0f} {'g' if unit == 'kg' else 'ml'}, "
                             f"pack says {drained or size_text}")
        elif pack_qty == 1 and unit in ("kg", "l"):
            check = "1 kg/1 L pack"
    elif len({u for _, u in page_units}) == 1 and page_units[0][1] in ("kg", "l") and price is not None:
        page_up, unit = page_units[0]
        unit_price, check = page_up, "page only"
        notes.append("unit price taken from the page")

    unit_price_total = unit_price
    if drained and price is not None and not by_weight:
        total = parse_size(size_text)
        unit_price_total = price / total[0] if total and total[1] == unit else None

    return {
        "product_id": product_id,
        "name": ld.get("name") or html.unescape(layer.get("item_name", "")),
        "brand": layer.get("item_brand", ""),
        "category": " / ".join(x for x in (layer.get("item_category2"), layer.get("item_category")) if x),
        "price": _fmt(price), "pre_discount_price": _fmt(pre) if on_promo else "",
        "discount": _fmt(pre - price) if on_promo else "",
        "on_promotion": "yes" if on_promo else "no", "price_valid_until": "",
        "sold_by": "kg" if by_weight or (unit == "kg" and pack_qty is None) else "", "size_text": size_text,
        "weight_basis": weight_basis, "pack_quantity": f"{pack_qty:g}" if pack_qty else "",
        "unit_price": _fmt(unit_price, 3), "unit_price_total": _fmt(unit_price_total, 3), "unit": unit,
        "page_unit_price": _fmt(page_up), "check": check, "note": "; ".join(notes),
    }


if __name__ == "__main__":
    if "--test" in sys.argv:
        path = sys.argv[sys.argv.index("--test") + 1]
        pid = re.search(r"(\d+)\.html?$", path)
        page = open(path, encoding="utf-8").read()
        for k, v in parse_product_page(page, pid.group(1) if pid else "").items():
            print(f"  {k:20} {v}")
    else:
        run("pingodoce", parse_product_page, "Pingo Doce")
