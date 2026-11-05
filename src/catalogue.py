"""Store catalogues (names + product IDs from the stores' sitemaps), and the products that count as each list item.

Usage (store = continente or pingodoce):
    python src/catalogue.py pingodoce build                # download the catalogue (a few requests)
    python src/catalogue.py pingodoce search leite meio    # look up products by words
    python src/catalogue.py pingodoce review               # for each list item, decide once which
                                                            # products count as that item (all brands, all sizes)
    python src/catalogue.py pingodoce review --redo        # decide again for every item (all products shown)
    python src/catalogue.py pingodoce review --redo ^granola   # ... or for one item
    (python src/continente_catalogue.py build/search/review still works: it is this script for Continente.)

Shopping list: data/shopping/list.csv with the columns
    item      plain words; every word must appear in a product's name, e.g. "leite meio gordo"
    quantity  how much I buy
    exclude   words that rule a product out, separated by ";", e.g. "sem lactose; achocolatado; iogurte"
    notes

Decisions: data/shopping/<store>_products.csv (item, product_id, name, url, decision = accepted/rejected).
Rejected products are remembered, so the next review only shows products that are new in the catalogue.

Where the catalogues come from:
    The stores' product sitemaps for search engines. Both shops run on the same platform (Salesforce
    Commerce Cloud), and each entry's URL holds the product's name and ID:
      Continente  https://www.continente.pt/sitemap_index.xml (6 product sitemaps)
                  .../produto/atum-posta-ao-natural-continente-equilibrio-3697795.html
      Pingo Doce  https://www.pingodoce.pt/home/sitemap_index.xml (2 product sitemaps, checked 2026-11-03)
                  .../home/produtos/leite-e-bebidas-vegetais/leite/.../leite-uht-meio-gordo-pingo-doce-48150.html
    No prices: those come from the product pages of accepted products only (src/fetch_continente_online.py).

data/catalogue/ is not committed (rebuilt on demand). Cost: no LLM calls.
"""

import csv
import shutil
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import requests

STORES = {
    "continente": {"index": "https://www.continente.pt/sitemap_index.xml",
                   "url_re": re.compile(r"/produto/([a-z0-9-]+)-(\d+)\.html")},
    "pingodoce": {"index": "https://www.pingodoce.pt/home/sitemap_index.xml",
                  "url_re": re.compile(r"/produtos/(?:[^/]+/)*([^/]+)-(\d+)\.html$")},
}
LIST = Path("data/shopping/list.csv")
DECISION_FIELDS = ["item", "product_id", "name", "url", "decision"]
STORE = "continente"          # set by main(); the paths below follow it


def catalogue_path(store: str | None = None) -> Path:
    return Path(f"data/catalogue/{store or STORE}.csv")


def decisions_path(store: str | None = None) -> Path:
    return Path(f"data/shopping/{store or STORE}_products.csv")


def script(store: str | None = None) -> str:
    return f"python src/catalogue.py {store or STORE}"


HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
MAX_SHOWN = 200          # 2026-10-26: 113 genuine granolas were over the old limit of 80
STOPWORDS = {"de", "da", "do", "das", "dos", "e", "com", "em", "a", "o", "ao", "para"}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", normalise(text)) if t and t not in STOPWORDS]


def word_matches(query_word: str, name_words: list[str]) -> bool:
    """'banana' matches 'bananas' and vice versa; at least 4 letters in common for the reverse case.
    Words of 1-2 letters must match exactly: 'm' (egg size M) must not match 'matinados' (a brand)."""
    if len(query_word) <= 2:
        return query_word in name_words
    # Reverse direction only for plurals: query 'bananas' matches name 'banana', but query 'granola' must
    # not match name 'gran' (2026-10-26: 'granola' matched 'gran reserva' rum and port wine).
    return any(n.startswith(query_word) or query_word in (n + "s", n + "es") for n in name_words)


# ---------- build ----------

def parse_sitemap(xml: str) -> list[dict]:
    rows = []
    for entry in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", entry, re.S)
        m = STORES[STORE]["url_re"].search(loc.group(1).strip()) if loc else None
        if not m:
            continue
        lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", entry, re.S)
        rows.append({"product_id": m.group(2), "name": unquote(m.group(1)).replace("-", " "),
                     "url": loc.group(1), "lastmod": lastmod.group(1) if lastmod else ""})
    return rows


def cmd_build() -> None:
    index = requests.get(STORES[STORE]["index"], headers=HEADERS, timeout=60).text
    sitemaps = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", index) if "-product" in u]
    print(f"{len(sitemaps)} product sitemaps")
    products: dict[str, dict] = {}
    for url in sitemaps:
        time.sleep(DELAY_S)
        rows = parse_sitemap(requests.get(url, headers=HEADERS, timeout=120).text)
        for r in rows:
            products.setdefault(r["product_id"], r)
        print(f"  {url.rsplit('/', 1)[-1]}: {len(rows)} products")
    catalogue_path().parent.mkdir(parents=True, exist_ok=True)
    with open(catalogue_path(), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "name", "url", "lastmod"])
        w.writeheader()
        w.writerows(sorted(products.values(), key=lambda r: r["name"]))
    print(f"{len(products)} unique products -> {catalogue_path()}")


# ---------- search ----------

def load_catalogue() -> list[dict]:
    if not catalogue_path().exists():
        sys.exit(f"{catalogue_path()} not found. Run: {script()} build")
    with open(catalogue_path(), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_tokens"] = tokens(r["name"])
        r["_norm"] = normalise(r["name"])
    return rows


def candidates(catalogue: list[dict], item: str, exclude: str) -> list[dict]:
    """Every product whose name contains ALL words of the item and none of the excluded phrases.
    An item starting with '^' must have its FIRST word at the start of the name; the other words can be
    anywhere: '^mirtilo' keeps 'mirtilo continente 125 g' but not 'doce de mirtilo' (2026-10-22: plain
    'mirtilos' matched 100 flavoured products), and '^capsulas cafe nicola' matches
    'capsulas de cafe ... int 10 nicola' (2026-10-26: anchoring every word found nothing)."""
    anchored = item.strip().startswith("^")
    q = tokens(item.strip().lstrip("^"))
    bans = [normalise(x).strip() for x in exclude.split(";") if x.strip()]

    def ok(r: dict) -> bool:
        if not q or any(b in r["_norm"] for b in bans):
            return False
        if anchored and not (r["_tokens"] and word_matches(q[0], r["_tokens"][:1])):
            return False
        return all(word_matches(t, r["_tokens"]) for t in q)

    return sorted((r for r in catalogue if ok(r)), key=lambda r: r["name"])


def cmd_search(query: str) -> None:
    for i, r in enumerate(candidates(load_catalogue(), query, ""), 1):
        print(f"{i}. {r['name']}  [{r['product_id']}]")


# ---------- review ----------

def parse_numbers(answer: str, n: int) -> set[int] | None:
    """'3 5 8-10' -> {3, 5, 8, 9, 10}; None if the answer isn't a list of numbers in range."""
    picked: set[int] = set()
    for part in answer.replace(",", " ").split():
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= a <= b <= n):
            return None
        picked.update(range(a, b + 1))
    return picked


def load_decisions() -> list[dict]:
    if not decisions_path().exists():
        return []
    with open(decisions_path(), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_decisions(rows: list[dict]) -> None:
    decisions_path().parent.mkdir(parents=True, exist_ok=True)
    with open(decisions_path(), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DECISION_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["item"], r["decision"], r["name"])))


def cmd_review(redo: str | None = None) -> None:
    """redo: '' = every list item, or one item's exact text. Its old decisions are dropped and every
    matching product is shown again, accepted ones included (backup of the file first). Products that
    were marked unavailable and are accepted again stay unavailable, so weekly runs keep skipping them."""
    catalogue = load_catalogue()
    with open(LIST, encoding="utf-8") as f:
        items = [r for r in csv.DictReader(f) if (r.get("item") or "").strip()]
    decisions = load_decisions()
    was_unavailable = set()
    if redo is not None:
        names = [r["item"].strip() for r in items]
        if redo and redo not in names:
            sys.exit(f"'{redo}' is not an item on {LIST}. Items: {', '.join(names)}")
        backup = decisions_path().with_suffix(".before-redo.csv")
        shutil.copy(decisions_path(), backup)
        print(f"backup of your decisions: {backup}")
        redo_items = {redo} if redo else set(names)
        was_unavailable = {(d["item"], d["product_id"]) for d in decisions
                           if d["item"] in redo_items and d["decision"] == "unavailable"}
        old = {it: [d for d in decisions if d["item"] == it] for it in redo_items}
        decisions = [d for d in decisions if d["item"] not in redo_items]
        items = [r for r in items if r["item"].strip() in redo_items]
    for row in items:
        item, exclude = row["item"].strip(), (row.get("exclude") or "").strip()
        # Accepted products must still match the item's CURRENT words and exclusions: an exclusion added
        # later, or a matching fix, removes them (2026-10-22: adding "frango" did not remove ready meals;
        # "ovos classe m" had matched "classe xl matinados" through the one-letter word "m").
        mine = [d for d in decisions if d["item"] == item and d["decision"] == "accepted"]
        as_catalogue = [{"product_id": d["product_id"], "name": d["name"], "_tokens": tokens(d["name"]),
                         "_norm": normalise(d["name"])} for d in mine]
        still = {r["product_id"] for r in candidates(as_catalogue, item, exclude)}
        newly_excluded = [d for d in mine if d["product_id"] not in still]
        for d in newly_excluded:
            d["decision"] = "rejected"
        if newly_excluded:
            save_decisions(decisions)
        seen = {d["product_id"] for d in decisions if d["item"] == item}
        accepted = sum(1 for d in decisions if d["item"] == item and d["decision"] == "accepted")
        new = [r for r in candidates(catalogue, item, exclude) if r["product_id"] not in seen]
        print(f"\n== {item}  (exclude: {exclude or '-'})  already accepted: {accepted}")
        for d in newly_excluded:
            print(f"   no longer matches this item, now rejected: {d['name']}")
        if not new:
            print("   nothing new")
            continue
        if len(new) > MAX_SHOWN:
            print(f"   {len(new)} new matches: too many to review. Add more words to the item, or "
                  f"phrases to 'exclude' in {LIST}, and run review again. First 15:")
            for r in new[:15]:
                print(f"     {r['name']}")
            continue
        for i, r in enumerate(new, 1):
            print(f"   {i:2}. {r['name']}  [{r['product_id']}]")
        while True:
            answer = input("   Enter = accept all; or numbers to REJECT (e.g. 3 5 8-10); s = skip item> ").strip()
            if answer.lower() == "s":
                rejected = None
                break
            rejected = parse_numbers(answer, len(new)) if answer else set()
            if rejected is not None:
                break
            print("   not understood, try again")
        if rejected is None:
            if redo is not None:                  # skipped during a redo: keep the old decisions
                decisions += old.get(item, [])
                save_decisions(decisions)
                print("   skipped: old decisions kept")
            continue
        for i, r in enumerate(new, 1):
            decision = "rejected" if i in rejected else "accepted"
            if decision == "accepted" and (item, r["product_id"]) in was_unavailable:
                decision = "unavailable"
            decisions.append({"item": item, "product_id": r["product_id"], "name": r["name"], "url": r["url"],
                              "decision": decision})
        save_decisions(decisions)
        print(f"   saved: {len(new) - len(rejected)} accepted, {len(rejected)} rejected")
    total = sum(1 for d in decisions if d["decision"] == "accepted")
    print(f"\n{total} accepted products in {decisions_path()} (= product pages fetched per weekly price run)")


def main(argv: list[str] | None = None) -> None:
    global STORE
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in STORES:
        sys.exit(__doc__)
    STORE = args.pop(0)
    if args == ["build"]:
        cmd_build()
    elif len(args) >= 2 and args[0] == "search":
        cmd_search(" ".join(args[1:]))
    elif args == ["review"]:
        cmd_review()
    elif len(args) >= 2 and args[:2] == ["review", "--redo"]:
        cmd_review(" ".join(args[2:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
