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
Known limitation: lidl.pt lists only offers, so an item without a Lidl offer this week can only be
bought at Continente in these plans, even if Lidl sells it at a regular price.
Cost: no LLM calls, no network.
"""

import csv
import sys
from datetime import date
from pathlib import Path

from continente_catalogue import candidates, normalise, tokens
from fetch_continente_online import comparable_prices

LIST = Path("data/shopping/list.csv")
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

    lines = [f"# Where to shop, {today}", "",
             f"Prices: Continente Online ({c_date or 'none'}), lidl.pt offers ({l_date or 'none'}). "
             f"Trip cost for a second store: {trip_cost:.2f} €."
             + ("" if use_plus else " Lidl Plus prices ignored."), "",
             "| Item | Quantity | Continente: cheapest | Lidl: cheapest offer | Buy at |",
             "|---|---|---|---|---|"]
    plan_a = plan_b = 0.0
    split_items, notes = [], []
    for it in items:
        name = it["item"].strip()
        qty = float(it.get("quantity") or 1)
        unit = (it.get("unit") or "").strip().lower()
        c_rows = [r | {"store": "Continente"} for r in cont if r.get("item") == name]
        l_rows = candidates(lidl_rows, name, it.get("exclude") or "")
        basis, ranked = comparable_prices(c_rows + l_rows)
        if unit and unit not in UNITS:
            notes.append(f"{name}: unit '{unit}' not understood (use l, kg or unit)")
            continue
        if unit:
            ranked = [(v, r) for v, r in ranked if r["unit"] == unit]
        best = {}
        for v, r in ranked:
            best.setdefault(r["store"], (v, r))
        if not best:
            lines.append(f"| {name} | {qty:g} {unit} | no price | no offer | — |")
            notes.append(f"{name}: no comparable price" + (f" in {unit}" if unit else ""))
            continue
        u = unit or next(iter(best.values()))[1]["unit"]
        if not unit:
            notes.append(f"{name}: no unit on the list; quantity {qty:g} read as {u}")

        def cell(store: str) -> str:
            if store not in best:
                return "no offer" if store == "Lidl" else "no price"
            v, r = best[store]
            extra = " (Lidl Plus)" if r.get("app") else (" (promo)" if r.get("on_promotion") == "yes" else "")
            return f"{v:.2f} €/{r['unit']} {r['name'][:40]}{extra}"

        c_cost = best["Continente"][0] * qty if "Continente" in best else None
        cheapest_store, (cheapest_v, _) = min(best.items(), key=lambda kv: kv[1][0])
        if c_cost is None:
            notes.append(f"{name}: no Continente price, so plan A can't include it")
        plan_a += c_cost or 0
        plan_b += cheapest_v * qty
        if cheapest_store != "Continente":
            split_items.append((name, (c_cost or 0) - cheapest_v * qty if c_cost else None))
        basis_note = " (total weight)" if basis.startswith("total") else ""
        lines.append(f"| {name}{basis_note} | {qty:g} {u} | {cell('Continente')} | {cell('Lidl')} | {cheapest_store} |")

    saving = plan_a - plan_b
    lines += ["", f"**Everything at Continente:** {plan_a:.2f} €  ",
              f"**Each item where it's cheapest:** {plan_b:.2f} € (saves {saving:.2f} €)", ""]
    if split_items and saving > trip_cost:
        lines.append(f"**Recommendation: split.** Buy {', '.join(n for n, _ in split_items)} at Lidl, the rest at "
                     f"Continente. Saves {saving:.2f} €, more than the {trip_cost:.2f} € trip cost.")
    elif split_items:
        lines.append(f"**Recommendation: Continente only.** Lidl would save {saving:.2f} € on "
                     f"{', '.join(n for n, _ in split_items)}, less than the {trip_cost:.2f} € trip cost.")
    else:
        lines.append("**Recommendation: Continente only.** No Lidl offer this week beats Continente.")
    lines += ["", "Costs are unit price × quantity and ignore pack rounding. Lidl prices are this week's offers "
              "only; items without an offer are priced at Continente. Continente prices are online prices."]
    if notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in notes]

    report = "\n".join(lines)
    print(report)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{today}.md").write_text(report + "\n", encoding="utf-8")
    print(f"\n-> {REPORTS / (today + '.md')}")


if __name__ == "__main__":
    main()
