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
                   {t('item')} AS item, {t('product_id')} AS product_id, {t('name')} AS name,
                   {t('brand')} AS brand, {t('category')} AS category,
                   {n('price')} AS price, {n('pre_discount_price')} AS pre_discount_price,
                   coalesce({t('on_promotion')} = 'yes', false) AS on_promotion,
                   {n('unit_price')} AS unit_price, {n('unit_price_total')} AS unit_price_total,
                   {t('unit')} AS unit, {t('weight_basis')} AS weight_basis, {t('size_text')} AS size_text,
                   {t('check')} AS price_check
            FROM {src}
            WHERE {t('item')} IS NOT NULL AND {t('item')} <> ''""")
    if parts:
        con.execute("CREATE VIEW shop_prices AS " + " UNION ALL ".join(parts))
    if list((PRICES / "lidl").glob("*.csv")):
        src = read(f"{PRICES}/lidl/*.csv")
        t, n, d = picker(src)
        con.execute(f"""CREATE VIEW lidl AS
            SELECT {SNAPSHOT} AS snapshot, {t('product_id')} AS product_id, {t('name')} AS name,
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


REPORT = [
    ("Snapshots per store", """
        SELECT store, count(DISTINCT snapshot) AS snapshots, min(snapshot) AS first, max(snapshot) AS last,
               count(*) AS product_prices
        FROM shop_prices GROUP BY store
        UNION ALL
        SELECT 'Lidl', count(DISTINCT snapshot), min(snapshot), max(snapshot), count(*) FROM lidl
        ORDER BY store"""),
    ("Cheapest unit price per item, store and snapshot (Continente and Pingo Doce)", """
        SELECT item, store, snapshot, min(unit_price) AS cheapest, any_value(unit) AS per
        FROM shop_prices WHERE unit_price > 0
        GROUP BY ALL ORDER BY item, store, snapshot"""),
    ("How often the cheapest product per item is on promotion", """
        WITH ranked AS (
            SELECT *, row_number() OVER (PARTITION BY store, snapshot, item ORDER BY unit_price) AS rank
            FROM shop_prices WHERE unit_price > 0)
        SELECT item, store, count(*) AS snapshots, sum(on_promotion::INT) AS cheapest_on_promo,
               round(100 * avg(on_promotion::INT)) AS pct
        FROM ranked WHERE rank = 1 GROUP BY ALL ORDER BY item, store"""),
    ("Products whose price changed between their first and latest snapshot (biggest moves)", """
        WITH ends AS (
            SELECT store, product_id, any_value(name) AS name, any_value(item) AS item,
                   arg_min(price, snapshot) AS first_price, arg_max(price, snapshot) AS last_price,
                   min(snapshot) AS first, max(snapshot) AS last
            FROM shop_prices WHERE price > 0 GROUP BY store, product_id)
        SELECT store, item, name, first, first_price, last, last_price,
               round(100 * (last_price / first_price - 1)) AS pct_change
        FROM ends WHERE first < last AND last_price <> first_price
        ORDER BY abs(last_price / first_price - 1) DESC LIMIT 10"""),
    ("Lidl offers per snapshot: discount against the price before the offer", """
        SELECT snapshot, count(*) AS products,
               count(old_price) FILTER (WHERE old_price > coalesce(lidl_plus_price, shelf_price)) AS with_discount,
               round(100 * avg(1 - coalesce(lidl_plus_price, shelf_price) / old_price)
                     FILTER (WHERE old_price > coalesce(lidl_plus_price, shelf_price))) AS avg_discount_pct,
               count(lidl_plus_price) AS lidl_plus
        FROM lidl GROUP BY snapshot ORDER BY snapshot"""),
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
