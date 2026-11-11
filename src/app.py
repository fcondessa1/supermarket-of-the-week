"""Supermarket of the Week: the app. Write your list, see which products count, find out where to shop.

Run locally:   streamlit run src/app.py         (secrets in .streamlit/secrets.toml, never committed)
Deployed:      Streamlit Community Cloud, main file src/app.py, secrets in the app's settings.

Secrets:
    [users]                      name = "password", one line per person (names: lowercase letters, digits, - _)
    ANTHROPIC_API_KEY = "..."    for judging new items with the model
    GITHUB_TOKEN = "..."         fine-grained token, Contents read/write on this repository only (deployed app)
    GITHUB_REPO = "owner/repo"
    DAILY_CAP_USD = 0.25         model spending per day, all users together (optional, default 0.25)

Prices come from the Tuesday run: an item added today is matched and judged at once, and priced next Tuesday.
"""

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)                                  # every path in the project is relative to the repository root
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import streamlit as st

import lists
from llm_match import load_cache, lookup
from recommend import compute, latest

st.set_page_config(page_title="Supermarket of the Week", page_icon="🛒", layout="wide")

SECRETS = {}
try:
    SECRETS = dict(st.secrets)
except Exception:                               # no secrets file: the login page explains what to add
    pass
for key in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO"):
    if key in SECRETS and not os.environ.get(key):
        os.environ[key] = str(SECRETS[key])
DAILY_CAP = float(SECRETS.get("DAILY_CAP_USD", 0.25))
UNITS = ["kg", "l", "unit"]


@st.cache_resource
def spending() -> dict:
    """Model spending per day, shared by every session of this app (resets if the app restarts: the hard limit
    is the monthly spend limit set in the Anthropic console)."""
    return {}


@st.cache_data(ttl=3600, show_spinner="Loading the product catalogues…")
def catalogues() -> dict:
    return lists.all_catalogues()


@st.cache_data(ttl=3600)
def latest_prices() -> tuple[dict, dict]:
    """(store, product id) -> (price, unit price, unit); and store -> date of its latest price file."""
    prices, dates = {}, {}
    for label, folder in lists.SHOPS.items():
        day, rows = latest(folder)
        dates[label] = day
        for r in rows:
            if r.get("price"):
                prices[(label, r["product_id"])] = (r["price"], r.get("unit_price_total") or r.get("unit_price"), r.get("unit"))
    day, rows = latest("lidl")
    dates["Lidl"] = day
    for r in rows:
        p = r.get("lidl_plus_price") or r.get("shelf_price")
        if p:
            prices[("Lidl", r.get("product_id") or r["name"])] = (p, r.get("unit_price_total") or r.get("unit_price"), r.get("unit"))
    return prices, dates


# ---------- login ----------

def login() -> str | None:
    if st.session_state.get("user"):
        return st.session_state["user"]
    people = SECRETS.get("users") or {}
    st.title("🛒 Supermarket of the Week")
    st.write("Where to shop this week, for your own list, across Continente, Pingo Doce and Lidl.")
    if not people:
        st.error("No users configured. Add a [users] section to the app's secrets (see src/app.py).")
        return None
    with st.form("login"):
        name = st.text_input("Name").strip().lower()
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if name in people and password == str(people[name]) and lists.NAME_RE.match(name):
                st.session_state["user"] = name
                st.rerun()
            st.error("Wrong name or password.")
    return None


# ---------- model judging, with the daily cap ----------

def judge_items(user_items: list[tuple[str, dict]]) -> None:
    cache = load_cache()
    todo = lists.unjudged(user_items, catalogues(), cache)
    if not todo:
        return
    spent = spending().get(date.today().isoformat(), 0.0)
    estimate = len(todo) * 0.00004                      # Haiku 5.5: about $0.00002-0.00004 per product judged
    if spent + estimate > DAILY_CAP:
        st.warning(f"{len(todo)} products are waiting for the model, but today's model budget "
                   f"(${DAILY_CAP:.2f}) is used up. They will be judged on Tuesday.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning(f"{len(todo)} products are waiting for the model (no ANTHROPIC_API_KEY set); they will be "
                   "judged on Tuesday.")
        return
    with st.spinner(f"Asking the model about {len(todo)} products…"):
        new, cost = lists.judge(todo, "haiku", cache)
        lists.push_cache(new)
    spending()[date.today().isoformat()] = spent + cost
    st.toast(f"Model judged {len(new)} products (${cost:.4f}).")


# ---------- pages ----------

def page_list(user: str) -> None:
    st.subheader("My list")
    st.caption("One line per thing you need, in plain Portuguese words: every word must be in the product's name "
               "(\"leite meio gordo sem lactose\"). Start with ^ when the product must BE that word, not just "
               "contain it (\"^granola\", not \"bolachas com granola\"). The **rule** says what doesn't count, in "
               "your own words: the model reads it for every product (\"not fresh milk\", \"not frozen; not "
               "freeze-dried\"). Notes are just for you.")
    rows = lists.load_list(user)
    df = pd.DataFrame(rows or [{"active": "yes", "item": "", "quantity": "", "unit": "kg", "rule": "", "notes": ""}],
                      columns=lists.LIST_FIELDS)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["active"] = df["active"] != "no"
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", hide_index=True, key="list_editor",
        column_config={
            "active": st.column_config.CheckboxColumn("This week", default=True,
                                                      help="Untick to leave it out this week without deleting it"),
            "item": st.column_config.TextColumn("Item", required=True, width="medium"),
            "quantity": st.column_config.NumberColumn("Quantity", min_value=0, step=0.05, format="%g"),
            "unit": st.column_config.SelectboxColumn("Unit", options=UNITS, required=True),
            "rule": st.column_config.TextColumn("Rule (what doesn't count)", width="large"),
            "notes": st.column_config.TextColumn("Notes (only for you)"),
        })
    if st.button("Save my list", type="primary"):
        new_rows = [r for r in edited.fillna("").to_dict("records") if str(r["item"]).strip()]
        for r in new_rows:
            r["quantity"] = f"{float(r['quantity']):g}" if r["quantity"] != "" else ""
            r["active"] = "no" if r["active"] is False else "yes"
        lists.save_list(user, new_rows)
        judge_items([(user, r) for r in lists.load_list(user)])
        st.success("Saved. New items are matched now and get prices in the next Tuesday run.")


def page_products(user: str) -> None:
    st.subheader("Which products count")
    items = lists.load_list(user)
    if not items:
        st.info("Your list is empty: add items under My list.")
        return
    item_names = [it["item"] for it in items]
    choice = st.selectbox("Item", item_names)
    it = next(i for i in items if i["item"] == choice)
    st.caption(f"Rule: {it['rule'] or '(none)'} · The model decides from the rule; untick or tick to overrule it, "
               "and your choice is kept from then on.")
    cands = lists.candidate_products(choice, catalogues())
    cache, overrides = load_cache(), lists.load_overrides(user)
    prices, _ = latest_prices()
    tabs = st.tabs([f"{s} ({len(c)})" for s, c in cands.items()])
    edits = {}
    for tab, (store, rows) in zip(tabs, cands.items()):
        with tab:
            if len(rows) > lists.MAX_CANDIDATES:
                st.warning(f"{len(rows)} products have these words; only the first {lists.MAX_CANDIDATES} are "
                           "judged. Add a word to the item to narrow it down.")
            table = []
            for r in rows[:lists.MAX_CANDIDATES]:
                key = r.get("product_id") or r["name"]
                a = lookup(cache, choice, it["rule"], r["name"])
                model = None if not a or a.get("match") is None else bool(a["match"])
                o = overrides.get((store, choice, key))
                p = prices.get((store, key))
                table.append({"counts": (o["counts"] == "yes") if o else bool(model),
                              "product": r["name"],
                              "model": "✓" if model else "✗" if model is False else "not judged",
                              "reason": "your choice" if o else (a or {}).get("reason", ""),
                              "price": f"{float(p[0]):.2f} €" if p else "",
                              "per kg/l/unit": f"{float(p[1]):.2f} €/{p[2]}" if p and p[1] else "",
                              "_key": key, "_model": model})
            if not table:
                st.write("No product has all these words.")
                continue
            df = pd.DataFrame(table).sort_values(["counts", "product"], ascending=[False, True])
            edits[store] = (df, st.data_editor(
                df.drop(columns=["_key", "_model"]), hide_index=True, width="stretch",
                disabled=["product", "model", "reason", "price", "per kg/l/unit"], key=f"prod_{store}_{choice}",
                column_config={"counts": st.column_config.CheckboxColumn("Counts", help="Untick to exclude")}))
    if edits and st.button("Save my corrections", type="primary"):
        today = date.today().isoformat()
        for store, (orig, edited) in edits.items():
            for (_, o_row), (_, e_row) in zip(orig.iterrows(), edited.iterrows()):
                key = (store, choice, o_row["_key"])
                if bool(e_row["counts"]) == o_row["_model"]:
                    overrides.pop(key, None)                 # agrees with the model again: no override needed
                elif bool(e_row["counts"]) != bool(o_row["counts"]) or key in overrides:
                    overrides[key] = {"store": store, "item": choice, "product_id": o_row["_key"],
                                      "name": o_row["product"], "counts": "yes" if e_row["counts"] else "no",
                                      "date": today}
        lists.save_overrides(user, overrides)
        st.success("Saved. Your corrections win over the model from now on.")
    unjudged = sum(1 for _, (df, _) in edits.items() for m in df["_model"] if m is None)
    if unjudged and st.button(f"Ask the model about the {unjudged} products not judged yet"):
        judge_items([(user, it)])
        st.rerun()


def page_where(user: str) -> None:
    st.subheader("Where to shop")
    items = lists.load_list(user, active_only=True)
    paused = [r["item"].lstrip("^") for r in lists.load_list(user) if r["active"] == "no"]
    if paused:
        st.caption("Not this week (unticked on My list): " + ", ".join(paused))
    if not items:
        st.info("Nothing ticked for this week: tick items under My list.")
        return
    c1, c2 = st.columns(2)
    trip = c1.number_input("A second shop is worth it only if it saves more than (€)", 0.0, 20.0, 3.0, 0.5)
    plus = c2.checkbox("I use the Lidl Plus app", value=True)
    result = compute(items, date.today().isoformat(), trip_cost=trip, use_plus=plus,
                     verdict=lists.verdict_for(user, load_cache()))
    st.markdown("\n".join(line for line in result["summary"] if not line.startswith("## ")))

    rows = result["comparison"]
    if rows:
        stores = list(dict.fromkeys(r["store"] for r in rows))
        table = []
        for item in dict.fromkeys(r["item"] for r in rows):
            line = {"item": item.lstrip("^")}
            for s in stores:
                r = next((r for r in rows if r["item"] == item and r["store"] == s), None)
                short = {"promotion": "promo", "offer": "offer", "Lidl Plus offer": "Lidl Plus",
                         "regular (my table)": "my price", "price before offer (lidl.pt)": "before offer"}
                line[s] = (("✅ " if r["chosen"] else "") + f"{float(r['cost']):.2f} € ({float(r['unit_price']):.2f} €/{r['unit']})"
                           + f" · {r['product'][:32]}"
                           + ("" if r["source"] == "regular" else f" · {short.get(r['source'], r['source'])}")) if r else "—"
            table.append(line)
        st.caption("Cost of your quantity at each shop's cheapest product that counts for you, with its price per "
                   "kg, litre or unit in brackets. ✅ = where the recommendation buys it.")
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
    if result["unpriced"]:
        st.info("No price yet for: " + ", ".join(i.lstrip("^") for i in result["unpriced"]) +
                ". Items added since the last Tuesday run get prices in the next one.")
    _, dates = latest_prices()
    st.caption("Prices: " + ", ".join(f"{s} {d or 'none'}" for s, d in dates.items()) +
               ". Continente and Pingo Doce are online prices; Lidl is this week's lidl.pt offers or a remembered "
               "price before an offer. Costs are unit price × quantity and ignore pack sizes.")
    if result["rejected"]:
        with st.expander(f"Not counted for you ({len(result['rejected'])} products)"):
            st.dataframe(pd.DataFrame(result["rejected"], columns=["item", "store", "product", "why"]),
                         hide_index=True, width="stretch")
    if result["notes"]:
        with st.expander("Notes"):
            st.markdown("\n".join(f"- {n}" for n in result["notes"]))


def main() -> None:
    user = login()
    if not user:
        return
    with st.sidebar:
        st.write(f"Signed in as **{user}**")
        page = st.radio("Page", ["Where to shop", "My list", "Which products count"])
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()
    {"Where to shop": page_where, "My list": page_list, "Which products count": page_products}[page](user)


main()
