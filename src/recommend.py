"""Where to shop this week: compare my list across stores and recommend one store, or a split.

Usage:
    python src/recommend.py                    # uses the latest price file of each store
    python src/recommend.py --trip-cost 5      # a second store must save more than 5 € (default 3 €)
    python src/recommend.py --no-lidl-plus     # ignore prices that need the Lidl Plus app

Inputs:
    data/shopping/list.csv          item, quantity, unit (l / kg / unit), exclude, notes
    data/prices/continente/*.csv    latest file: every accepted product for each list item
    data/prices/lidl/*.csv          latest file: this week's Lidl offers (matched to items here, same word rules)
Output:
    data/reports/<date>.md          the recommendation, in Markdown (also printed)

How it decides:
    1. For each item and store, the cheapest product per kg / L / unit, on one comparable basis across
       BOTH stores (if any product lacks a drained weight, all are compared on total weight).
    2. Cost of an item = unit price x the quantity on my list. This ignores pack rounding: 0.5 kg of
       granola may mean buying a 375 g or 600 g pack.
    3. Plans, all costed on the same items: everything at Continente, everything at Lidl (each only if that
       store has a price for all of them), or each item where it's cheapest plus the trip cost.
Lidl's regular prices, two sources (all compared; the cheapest per unit wins, and the report says which):
    a) lidl.pt itself: every weekly file is kept, and each offer shows the price before the offer
       (old_price, or the shelf price when the offer is a Lidl Plus price). The latest one seen per product
       is used, also after the offer ends; flagged after 60 days.
    b) my own table, below.
lidl.pt lists only this week's offers (248 products on 2026-10-27, no lactose-free
milk or basmati rice although Lidl sells both). Regular prices come from data/shopping/lidl_regular.csv,
which I fill in by hand from the shelf or my Lidl Plus receipts:
    item, product, price, quantity, unit, seen_on, notes
    leite meio gordo sem lactose, Milbona ... 1 L, 0.95, 1, l, 2026-10-27,
A price older than 60 days is still used but flagged as stale.
With those, a third plan is possible: everything at Lidl.
Cost: no LLM calls, no network.
"""

import csv
import sys
from datetime import date
from pathlib import Path

from continente_catalogue import candidates, normalise, tokens
from fetch_continente_online import comparable_prices

LIST = Path("data/shopping/list.csv")
LIDL_REGULAR = Path("data/shopping/lidl_regular.csv")
STALE_DAYS = 60
PRICES = Path("data/prices")
REPORTS = Path("data/reports")
UNITS = {"l", "kg", "unit"}


def latest(store: str) -> tuple[str, list[dict]]:
    files = sorted((PRICES / store).glob("*.csv"))
    if not files:
        return "", []
    with open(files[-1], encoding="utf-8") as f:
        return files[-1].stem, list(csv.DictReader(f))


def arg_value(name: str, default: float) -> float:
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return default


def lidl_valid_today(r: dict, today: str) -> bool:
    return (not r.get("start_date") or r["start_date"] <= today) and (not r.get("end_date") or today <= r["end_date"])


def lidl_regular_prices(today: str) -> tuple[dict[str, dict], list[str]]:
    """item -> row shaped like a price row (unit price per l/kg/unit), from my hand-kept table."""
    out, notes = {}, []
    if not LIDL_REGULAR.exists():
        return out, notes
    with open(LIDL_REGULAR, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            item = (r.get("item") or "").strip()
            try:
                price, qty = float(r["price"]), float(r["quantity"])
            except (KeyError, TypeError, ValueError):
                if item and (r.get("price") or "").strip():
                    notes.append(f"lidl_regular.csv: '{item}' has a price or quantity that isn't a number")
                continue
            unit = (r.get("unit") or "").strip().lower()
            seen = (r.get("seen_on") or "").strip()
            try:
                age = (date.fromisoformat(today) - date.fromisoformat(seen)).days
            except ValueError:
                age = None
            if age is not None and age > STALE_DAYS:
                notes.append(f"{item}: Lidl regular price is from {seen} ({age} days ago); check it again")
            value = f"{price / qty:.3f}"
            out[item] = {"store": "Lidl", "name": (r.get("product") or item).strip(), "price": f"{price:.2f}",
                         "unit": unit, "unit_price": value, "unit_price_total": value, "weight_basis": "",
                         "regular": seen or "date unknown", "source": "my table"}
    return out, notes


def regular_from_row(r: dict) -> float | None:
    """The price without any offer, as lidl.pt shows it, or None if the page doesn't say.
    - a crossed-out price (old_price) is the price before the offer;
    - with a Lidl Plus offer, the shelf price is the price without the app;
    - a shelf price alone may itself be the offer price, so it isn't used."""
    def num(k):
        try:
            return float(r.get(k) or "")
        except ValueError:
            return None
    old, shelf, plus = num("old_price"), num("shelf_price"), num("lidl_plus_price")
    if old:
        return old
    if plus and shelf and shelf > plus:
        return shelf
    return None


def lidl_history_regular(today: str) -> tuple[list[dict], int]:
    """Regular prices from every past lidl.pt file: for each product, the latest one seen.
    Every product that was ever on offer leaves its price before the offer; it stays usable after the offer ends."""
    latest_seen: dict[str, dict] = {}
    files = sorted((PRICES / "lidl").glob("*.csv"))
    for path in files:                      # oldest first, so later files overwrite
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                regular = regular_from_row(r)
                try:
                    unit_price = float(r.get("unit_price") or "")
                    total = float(r.get("unit_price_total") or r.get("unit_price") or "")
                    paid = float(r.get("lidl_plus_price") or r.get("shelf_price") or "")
                except ValueError:
                    continue
                if not regular or not r.get("name") or not paid:
                    continue
                factor = regular / paid           # the unit price was computed on the price paid
                seen = r.get("date") or path.stem
                latest_seen[r.get("product_id") or r["name"]] = {
                    "store": "Lidl", "name": r["name"], "price": f"{regular:.2f}", "unit": r.get("unit", ""),
                    "unit_price": f"{unit_price * factor:.3f}", "unit_price_total": f"{total * factor:.3f}",
                    "weight_basis": r.get("weight_basis", ""), "regular": seen, "source": "lidl.pt",
                    "_tokens": tokens(r["name"]), "_norm": normalise(r["name"])}
    return list(latest_seen.values()), len(files)


def main() -> None:
    trip_cost = arg_value("--trip-cost", 3.0)
    use_plus = "--no-lidl-plus" not in sys.argv
    today = date.today().isoformat()

    with open(LIST, encoding="utf-8") as f:
        items = [r for r in csv.DictReader(f) if (r.get("item") or "").strip()]
    c_date, cont = latest("continente")
    l_date, lidl = latest("lidl")

    # Lidl rows: this week's valid offers, with the price I would pay (Lidl Plus or not) as 'price'.
    lidl_rows = []
    for r in lidl:
        if not r.get("name") or not lidl_valid_today(r, today):
            continue
        plus = r.get("lidl_plus_price")
        if plus and not use_plus:
            continue          # its unit price was computed on the app price; skip rather than guess the other
        lidl_rows.append(r | {"store": "Lidl", "_tokens": tokens(r["name"]), "_norm": normalise(r["name"]),
                              "price": plus or r.get("shelf_price", ""), "app": "yes" if plus else ""})

    regular, notes = lidl_regular_prices(today)
    history, n_files = lidl_history_regular(today)
    lines = [f"# Where to shop, {today}", "",
             f"Prices: Continente Online ({c_date or 'none'}), lidl.pt offers ({l_date or 'none'}), "
             f"Lidl regular prices from my own table ({len(regular)} items) and from lidl.pt's price before offer "
             f"({len(history)} products seen in {n_files} weekly files). "
             f"Trip cost for a second store: {trip_cost:.2f} €."
             + ("" if use_plus else " Lidl Plus prices ignored."), "",
             "| Item | Quantity | Continente: cheapest | Lidl: cheapest (offer or regular) | Cheapest at |",
             "|---|---|---|---|---|"]
    STORES = ("Continente", "Lidl")
    costs: dict[str, dict[str, float]] = {}       # item -> store -> cost of my quantity at its cheapest product
    for it in items:
        name = it["item"].strip()
        qty = float(it.get("quantity") or 1)
        unit = (it.get("unit") or "").strip().lower()
        if unit and unit not in UNITS:
            notes.append(f"{name}: unit '{unit}' not understood (use l, kg or unit)")
            continue
        c_rows = [r | {"store": "Continente"} for r in cont if r.get("item") == name]
        l_rows = candidates(lidl_rows, name, it.get("exclude") or "")
        l_rows += candidates(history, name, it.get("exclude") or "")
        if name in regular:
            l_rows = l_rows + [regular[name]]
        basis, ranked = comparable_prices(c_rows + l_rows)
        if unit:
            ranked = [(v, r) for v, r in ranked if r["unit"] == unit]
        best = {}
        for v, r in ranked:
            best.setdefault(r["store"], (v, r))
        if not best:
            lines.append(f"| {name} | {qty:g} {unit} | no price | no price | — |")
            notes.append(f"{name}: no comparable price" + (f" in {unit}" if unit else ""))
            continue
        u = unit or next(iter(best.values()))[1]["unit"]
        if not unit:
            notes.append(f"{name}: no unit on the list; quantity {qty:g} read as {u}")
        if "Lidl" not in best:
            notes.append(f"{name}: no Lidl price (no offer this week, never seen on lidl.pt, not in lidl_regular.csv)")
        elif best["Lidl"][1].get("regular"):
            seen = best["Lidl"][1]["regular"]
            try:
                age = (date.fromisoformat(today) - date.fromisoformat(seen)).days
            except ValueError:
                age = None
            if best["Lidl"][1].get("source") == "lidl.pt" and age is not None and age > STALE_DAYS:
                notes.append(f"{name}: Lidl price before offer is from {seen} ({age} days ago); check it again")

        def cell(store: str) -> str:
            if store not in best:
                return "no price"
            v, r = best[store]
            if r.get("source") == "lidl.pt":
                extra = f" (price before offer, lidl.pt {r['regular']})"
            elif r.get("regular"):
                extra = f" (regular, my table, {r['regular']})"
            elif r.get("app"):
                extra = " (Lidl Plus offer)" if store == "Lidl" else ""
            elif store == "Lidl":
                extra = " (offer)"
            else:
                extra = " (promo)" if r.get("on_promotion") == "yes" else ""
            return f"{v:.2f} €/{r['unit']} {r['name'][:40]}{extra}"

        costs[name] = {store: v * qty for store, (v, _) in best.items()}
        cheapest_store = min(best, key=lambda st: best[st][0])
        basis_note = " (total weight)" if basis.startswith("total") else ""
        lines.append(f"| {name}{basis_note} | {qty:g} {u} | {cell('Continente')} | {cell('Lidl')} | {cheapest_store} |")

    # Plans, all costed on the same items (those priced at one store at least).
    # A one-store plan is possible only if that store has a price for every such item.
    # The split pays the trip cost, so it wins only if it saves more than that.
    plans: dict[str, float] = {}
    for store in STORES:
        if costs and all(store in c for c in costs.values()):
            plans[f"Everything at {store}"] = sum(c[store] for c in costs.values())
    at = {name: min(c, key=c.get) for name, c in costs.items()}
    uses_both = len(set(at.values())) == 2
    split_cost = sum(min(c.values()) for c in costs.values())
    if uses_both:
        plans["Split"] = split_cost + trip_cost
    lines.append("")
    for store in STORES:
        key = f"Everything at {store}"
        missing = [n for n, c in costs.items() if store not in c]
        lines.append(f"**{key}:** " + (f"{plans[key]:.2f} €  " if key in plans
                     else f"not possible (no {store} price for {', '.join(missing)})  "))
    if uses_both:
        lines.append(f"**Split:** {split_cost:.2f} € + {trip_cost:.2f} € trip cost = {plans['Split']:.2f} €  ")
    lines.append("")
    if plans:
        choice = min(plans, key=plans.get)
        others = sorted(v for k, v in plans.items() if k != choice)
        margin = f" It beats the next option by {others[0] - plans[choice]:.2f} €." if others else ""
        if choice == "Split":
            lidl_items = [n for n, st in at.items() if st == "Lidl"]
            lines.append(f"**Recommendation: split.** Buy {', '.join(lidl_items)} at Lidl, the rest at Continente.{margin}")
        else:
            lines.append(f"**Recommendation: {choice[0].lower() + choice[1:]}.**{margin}")
    else:
        lines.append("**No recommendation:** no item has a price at either store.")
    unpriced = [it["item"].strip() for it in items if it["item"].strip() not in costs]
    if unpriced:
        lines.append(f"Not in any plan (no price at either store): {', '.join(unpriced)}.")
    lines += ["", "Costs are unit price × quantity and ignore pack rounding. Continente prices are online prices. "
              "Lidl prices are this week's lidl.pt offers, otherwise a regular price: my own record, or the price lidl.pt showed "
              "before an earlier offer (the product may have been a limited-time item)."]
    if notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in notes]

    report = "\n".join(lines)
    print(report)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{today}.md").write_text(report + "\n", encoding="utf-8")
    print(f"\n-> {REPORTS / (today + '.md')}")


if __name__ == "__main__":
    main()