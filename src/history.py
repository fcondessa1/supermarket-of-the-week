"""My price history as SQL tables (DuckDB), read straight from the weekly CSV files.

Usage:
    python src/history.py schema                 # tables and columns
    python src/history.py report                 # standing analyses (coverage, cheapest per week, promotions, ...)
    python src/history.py sql "SELECT ..."       # any query

Nothing is copied: every run reads data/prices/*/*.csv, data/reports/*.csv and data/shopping/list.csv, so
the CSV files stay the only source and the database can't go stale. Each file is one snapshot; its date is
the file name. (Several snapshots can fall in one week: the Tuesday run plus my own runs.)

Tables:
    shop_prices   Continente and Pingo Doce: every accepted product of every list item, per snapshot.
                  unit_price is per kg / L / unit, on the drained weight where the label states one (tuna);
                  unit_price_total is always on the total weight. Compare tuna across products on the latter.
    lidl          lidl.pt: every product page (this week's offers), per snapshot
    comparison    recommend.py's per-item result: the cheapest product per store, with its source and whether
                  the recommended plan buys it there (from 2026-11-09; earlier weeks only have the .md report)
    shopping_list my list as it is now
Columns worth knowing: week (Monday of the snapshot's week); regular_price (price before any discount);
accepted_now (the product counts for the item under my current decisions).
This is also what the model gets in option 3 (questions in plain words, answered with SQL).
Cost: no LLM calls, no network.
"""

import sys
from pathlib import Path

import duckdb

PRICES = Path("data/prices")
SNAPSHOT = r"regexp_extract(filename, '(\d{4}-\d{2}-\d{2})\.csv$', 1)::DATE"


def read(glob: str) -> str:
    return (f"read_csv('{glob}', union_by_name = true, filename = true, all_varchar = true, header = true)")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()

    def columns(src: str) -> set[str]:
        return {c[0] for c in con.sql(f"DESCRIBE SELECT * FROM {src}").fetchall()}

    def picker(src: str):
        # Files written by older versions of the scripts lack some columns: those read as NULL.
        have = columns(src)
        text = lambda c: f'"{c}"' if c in have else "NULL"
        num = lambda c: f"TRY_CAST(NULLIF(\"{c}\", '') AS DOUBLE)" if c in have else "NULL::DOUBLE"
        day = lambda c: f"TRY_CAST(NULLIF(\"{c}\", '') AS DATE)" if c in have else "NULL::DATE"
        return text, num, day

    shops = [s for s in ("continente", "pingodoce") if list((PRICES / s).glob("*.csv"))]
    parts = []
    for s in shops:
        src = read(f"{PRICES}/{s}/*.csv")
        t, n, _ = picker(src)
        parts.append(f"""
            SELECT '{'Continente' if s == 'continente' else 'Pingo Doce'}' AS store, {SNAPSHOT} AS snapshot,
                   date_trunc('week', {SNAPSHOT})::DATE AS week,
                   {t('item')} AS item, {t('product_id')} AS product_id, {t('name')} AS name,
                   {t('brand')} AS brand, {t('category')} AS category,
                   {n('price')} AS price, {n('pre_discount_price')} AS pre_discount_price,
                   coalesce({n('pre_discount_price')}, {n('price')}) AS regular_price,
                   coalesce({t('on_promotion')} = 'yes', false) AS on_promotion,
                   {n('unit_price')} AS unit_price,
                   coalesce({n('unit_price_total')}, {n('unit_price')}) AS unit_price_total,
                   {t('unit')} AS unit, {t('weight_basis')} AS weight_basis, {t('size_text')} AS size_text,
                   {t('check')} AS price_check
            FROM {src}
            WHERE {t('item')} IS NOT NULL AND {t('item')} <> ''""")
    if parts:
        # accepted_now: the product counts for the item under my CURRENT decisions. Analyses use it, so products
        # I rejected later (frozen blueberries, deli chicken) don't distort the history they were once part of.
        decisions = " UNION ALL ".join(
            f"SELECT '{'Continente' if s == 'continente' else 'Pingo Doce'}' AS store, item, product_id, decision "
            f"FROM read_csv('data/shopping/{s}_products.csv', all_varchar = true, header = true)" for s in shops)
        # one row per product: a decisions file can list a product twice for an item (seen in testing)
        con.execute(f"""CREATE VIEW shop_prices AS
            SELECT p.*, coalesce(d.accepted, false) AS accepted_now
            FROM ({" UNION ALL ".join(parts)}) p
            LEFT JOIN (SELECT store, item, product_id, bool_or(decision IN ('accepted', 'unavailable')) AS accepted
                       FROM ({decisions}) GROUP BY ALL) d USING (store, item, product_id)""")
    if list((PRICES / "lidl").glob("*.csv")):
        src = read(f"{PRICES}/lidl/*.csv")
        t, n, d = picker(src)
        con.execute(f"""CREATE VIEW lidl AS
            SELECT {SNAPSHOT} AS snapshot, date_trunc('week', {SNAPSHOT})::DATE AS week,
                   {t('product_id')} AS product_id, {t('name')} AS name,
                   {t('category')} AS category, {n('shelf_price')} AS shelf_price, {n('old_price')} AS old_price,
                   {n('lidl_plus_price')} AS lidl_plus_price, {d('start_date')} AS start_date, {d('end_date')} AS end_date,
                   {n('unit_price')} AS unit_price, {t('unit')} AS unit
            FROM {src}
            WHERE {t('name')} <> '' AND ({n('shelf_price')} IS NOT NULL OR {n('lidl_plus_price')} IS NOT NULL)""")
    if list(Path("data/reports").glob("*.csv")):
        src = read("data/reports/*.csv")
        t, n, _ = picker(src)
        con.execute(f"""CREATE VIEW comparison AS
            SELECT {SNAPSHOT} AS snapshot, {t('item')} AS item, {n('quantity')} AS quantity, {t('unit')} AS unit,
                   {t('store')} AS store, {n('unit_price')} AS unit_price, {n('cost')} AS cost,
                   {t('product')} AS product, {t('source')} AS source, {t('chosen')} = 'yes' AS chosen
            FROM {src}""")
    con.execute("""CREATE VIEW shopping_list AS
        SELECT item, TRY_CAST(NULLIF(quantity, '') AS DOUBLE) AS quantity, unit, exclude, rule
        FROM read_csv('data/shopping/list.csv', all_varchar = true, header = true)""")
    return con


def show(con, sql: str, title: str = "") -> None:
    if title:
        print(f"\n== {title}")
    rel = con.sql(sql)
    rows = rel.fetchall()
    cols = rel.columns
    cells = [[("" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)) for v in r] for r in rows]
    widths = [min(42, max([len(c)] + [len(r[i]) for r in cells])) for i, c in enumerate(cols)]
    print("  " + "  ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    for r in cells:
        print("  " + "  ".join(v[:w].ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("  (no rows)")


# All analyses: products I accept NOW, per week (several snapshots can fall in one week: the lowest wins), and
# prices per TOTAL weight, which every version of the scripts recorded (unit_price switched to drained weight
# for tuna on 2026-10-22, which looked like a 35% price rise in the first version of this report).
REPORT = [
    ("Snapshots per store", """
        SELECT store, count(DISTINCT snapshot) AS snapshots, count(DISTINCT week) AS weeks,
               min(snapshot) AS first, max(snapshot) AS last, count(*) AS product_prices
        FROM shop_prices GROUP BY store
        UNION ALL
        SELECT 'Lidl', count(DISTINCT snapshot), count(DISTINCT week), min(snapshot), max(snapshot), count(*) FROM lidl
        ORDER BY store"""),
    ("Cheapest price per item, store and week (per kg / L / unit, total weight; products I accept now)", """
        SELECT item, store, week, min(unit_price_total) AS cheapest, any_value(unit) AS per
        FROM shop_prices WHERE accepted_now AND unit_price_total > 0
        GROUP BY ALL ORDER BY item, store, week"""),
    ("How often each item's cheapest product is on promotion (weeks)", """
        WITH weekly AS (
            SELECT store, week, item, arg_min(on_promotion, unit_price_total) AS cheapest_on_promo
            FROM shop_prices WHERE accepted_now AND unit_price_total > 0 GROUP BY ALL)
        SELECT item, store, count(*) AS weeks, sum(cheapest_on_promo::INT) AS on_promo,
               round(100 * avg(cheapest_on_promo::INT))::INT AS pct
        FROM weekly GROUP BY ALL ORDER BY item, store"""),
    ("Regular prices that changed (price before discount, so promotions starting or ending don't count)", """
        WITH ends AS (
            SELECT store, product_id, any_value(name) AS name, any_value(item) AS item,
                   arg_min(regular_price, snapshot) AS first_price, arg_max(regular_price, snapshot) AS last_price,
                   min(snapshot) AS first, max(snapshot) AS last
            FROM shop_prices WHERE accepted_now AND regular_price > 0 GROUP BY store, product_id)
        SELECT store, item, name, first, first_price, last, last_price,
               round(100 * (last_price / first_price - 1))::INT AS pct_change
        FROM ends WHERE first < last AND abs(last_price - first_price) >= 0.01
        ORDER BY abs(last_price / first_price - 1) DESC LIMIT 10"""),
    ("Lidl offers per week: discount against the price before the offer", """
        WITH weekly AS (SELECT DISTINCT ON (week, product_id) * FROM lidl ORDER BY week, product_id, snapshot DESC)
        SELECT week, count(*) AS products,
               count(old_price) FILTER (WHERE old_price > coalesce(lidl_plus_price, shelf_price)) AS with_discount,
               round(100 * avg(1 - coalesce(lidl_plus_price, shelf_price) / old_price)
                     FILTER (WHERE old_price > coalesce(lidl_plus_price, shelf_price)))::INT AS avg_discount_pct,
               count(lidl_plus_price) AS lidl_plus
        FROM weekly GROUP BY week ORDER BY week"""),
]


def main() -> None:
    args = sys.argv[1:]
    con = connect()
    if args == ["schema"]:
        for (t,) in con.sql("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall():
            cols = con.sql(f"DESCRIBE {t}").fetchall()
            print(f"{t}: " + ", ".join(f"{c[0]} {c[1]}" for c in cols))
    elif args == ["report"]:
        tables = {t for (t,) in con.sql("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
        for title, sql in REPORT:
            needed = {"lidl"} if "FROM lidl" in sql else {"shop_prices"}
            if "UNION ALL" in sql:
                needed = {"shop_prices", "lidl"}
            if needed <= tables:
                show(con, sql, title)
    elif len(args) == 2 and args[0] == "sql":
        show(con, args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
