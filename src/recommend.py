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
    3. Plan A: everything at Continente. Plan B: each item where it is cheapest. B is recommended only if
       it saves more than the trip cost.
Lidl's regular prices: lidl.pt lists only this week's offers (248 products on 2026-10-27, no lactose-free
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
                         "regular": seen or "date unknown"}
    return out, notes


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
    lines = [f"# Where to shop, {today}", "",
             f"Prices: Continente Online ({c_date or 'none'}), lidl.pt offers ({l_date or 'none'}), "
             f"Lidl regular prices from my own table ({len(regular)} items). "
             f"Trip cost for a second store: {trip_cost:.2f} €."
             + ("" if use_plus else " Lidl Plus prices ignored."), "",
             "| Item | Quantity | Continente: cheapest | Lidl: cheapest (offer or regular) | Cheapest at |",
             "|---|---|---|---|---|"]
    totals = {"Continente": 0.0, "Lidl": 0.0}
    complete = {"Continente": True, "Lidl": True}
    plan_split, split_lidl = 0.0, []
    for it in items:
        name = it["item"].strip()
        qty = float(it.get("quantity") or 1)
        unit = (it.get("unit") or "").strip().lower()
        if unit and unit not in UNITS:
            notes.append(f"{name}: unit '{unit}' not understood (use l, kg or unit)")
            continue
        c_rows = [r | {"store": "Continente"} for r in cont if r.get("item") == name]
        l_rows = candidates(lidl_rows, name, it.get("exclude") or "")
        if name in regular:
            l_rows = l_rows + [regular[name]]
        basis, ranked = comparable_prices(c_rows + l_rows)
        if unit:
            ranked = [(v, r) for v, r in ranked if r["unit"] == unit]
        best = {}
        for v, r in ranked:
            best.setdefault(r["store"], (v, r))
        for store in totals:
            if store in best:
                totals[store] += best[store][0] * qty
            else:
                complete[store] = False
        if not best:
            lines.append(f"| {name} | {qty:g} {unit} | no price | no price | — |")
            notes.append(f"{name}: no comparable price" + (f" in {unit}" if unit else ""))
            continue
        u = unit or next(iter(best.values()))[1]["unit"]
        if not unit:
            notes.append(f"{name}: no unit on the list; quantity {qty:g} read as {u}")
        if "Lidl" not in best:
            notes.append(f"{name}: no Lidl price (no offer this week, not in lidl_regular.csv)")

        def cell(store: str) -> str:
            if store not in best:
                return "no price"
            v, r = best[store]
            if r.get("regular"):
                extra = f" (regular, seen {r['regular']})"
            elif r.get("app"):
                extra = " (Lidl Plus offer)" if store == "Lidl" else ""
            elif store == "Lidl":
                extra = " (offer)"
            else:
                extra = " (promo)" if r.get("on_promotion") == "yes" else ""
            return f"{v:.2f} €/{r['unit']} {r['name'][:40]}{extra}"

        cheapest_store, (cheapest_v, _) = min(best.items(), key=lambda kv: kv[1][0])
        plan_split += cheapest_v * qty
        if cheapest_store == "Lidl":
            split_lidl.append(name)
        basis_note = " (total weight)" if basis.startswith("total") else ""
        lines.append(f"| {name}{basis_note} | {qty:g} {u} | {cell('Continente')} | {cell('Lidl')} | {cheapest_store} |")

    # Plans: one store (only if it has a price for every item), or a split (only worth it above the trip cost).
    plans = {}
    for store in totals:
        if complete[store]:
            plans[f"Everything at {store}"] = totals[store]
    uses_both = bool(split_lidl) and len(split_lidl) < len(items)
    if uses_both:
        plans["Split: each item where it's cheapest (+ trip cost)"] = plan_split + trip_cost
    lines += [""]
    for store in totals:
        lines.append(f"**Everything at {store}:** " + (f"{totals[store]:.2f} €  " if complete[store]
                     else f"not possible (no {store} price for some items)  "))
    if uses_both:
        lines.append(f"**Split:** {plan_split:.2f} € + {trip_cost:.2f} € trip cost = {plan_split + trip_cost:.2f} €  ")
    lines.append("")
    if plans:
        choice = min(plans, key=plans.get)
        if choice.startswith("Split"):
            lines.append(f"**Recommendation: split.** Buy {', '.join(split_lidl)} at Lidl, the rest at Continente.")
        else:
            runner_up = sorted(plans.values())[1] if len(plans) > 1 else None
            margin = f" It beats the next option by {runner_up - plans[choice]:.2f} €." if runner_up else ""
            lines.append(f"**Recommendation: {choice[0].lower() + choice[1:]}.**{margin}")
    else:
        lines.append("**No recommendation:** neither store has a price for every item, and there's nothing to split.")
    lines += ["", "Costs are unit price × quantity and ignore pack rounding. Continente prices are online prices. "
              "Lidl prices are this week's lidl.pt offers, or my own record of the regular price when there's no offer."]
    if notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in notes]

    report = "\n".join(lines)
    print(report)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{today}.md").write_text(report + "\n", encoding="utf-8")
    print(f"\n-> {REPORTS / (today + '.md')}")


if __name__ == "__main__":
    main()
