"""The full Continente Online catalogue (names + product IDs), and the products that count as each list item.

Usage:
    python src/continente_catalogue.py build                # download the catalogue (7 requests)
    python src/continente_catalogue.py search leite meio    # look up products by words
    python src/continente_catalogue.py review               # for each list item, decide once which
                                                            # products count as that item (all brands, all sizes)

Shopping list: data/shopping/list.csv with the columns
    item      plain words; every word must appear in a product's name, e.g. "leite meio gordo"
    quantity  how much I buy (used later by the optimiser)
    exclude   words that rule a product out, separated by ";", e.g. "sem lactose; achocolatado; iogurte"
    notes

Decisions: data/shopping/continente_products.csv (item, product_id, name, url, decision = accepted/rejected).
Rejected products are remembered, so the next review only shows products that are new in the catalogue.

Where the catalogue comes from:
    Continente's product sitemaps for search engines (https://www.continente.pt/sitemap_index.xml).
    Each entry's URL holds the product's name and ID. No prices: those come from the product pages
    of accepted products only (src/fetch_continente_online.py). Fetching every product page in the
    catalogue would be hours of requests and many GB every week, for things I never buy.

data/catalogue/ is not committed (rebuilt on demand). Cost: no LLM calls.
"""

import csv
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

INDEX = "https://www.continente.pt/sitemap_index.xml"
CATALOGUE = Path("data/catalogue/continente.csv")
LIST = Path("data/shopping/list.csv")
DECISIONS = Path("data/shopping/continente_products.csv")
DECISION_FIELDS = ["item", "product_id", "name", "url", "decision"]
HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
MAX_SHOWN = 200          # 2026-10-26: 113 genuine granolas were over the old limit of 80
URL_RE = re.compile(r"/produto/([a-z0-9-]+)-(\d+)\.html")
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
        m = URL_RE.search(loc.group(1)) if loc else None
        if not m:
            continue
        lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", entry, re.S)
        rows.append({"product_id": m.group(2), "name": m.group(1).replace("-", " "),
                     "url": loc.group(1), "lastmod": lastmod.group(1) if lastmod else ""})
    return rows


def cmd_build() -> None:
    index = requests.get(INDEX, headers=HEADERS, timeout=60).text
    sitemaps = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", index) if "-product" in u]
    print(f"{len(sitemaps)} product sitemaps")
    products: dict[str, dict] = {}
    for url in sitemaps:
        time.sleep(DELAY_S)
        rows = parse_sitemap(requests.get(url, headers=HEADERS, timeout=120).text)
        for r in rows:
            products.setdefault(r["product_id"], r)
        print(f"  {url.rsplit('/', 1)[-1]}: {len(rows)} products")
    CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOGUE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "name", "url", "lastmod"])
        w.writeheader()
        w.writerows(sorted(products.values(), key=lambda r: r["name"]))
    print(f"{len(products)} unique products -> {CATALOGUE}")


# ---------- search ----------

def load_catalogue() -> list[dict]:
    if not CATALOGUE.exists():
        sys.exit(f"{CATALOGUE} not found. Run: python src/continente_catalogue.py build")
    with open(CATALOGUE, encoding="utf-8") as f:
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
    if not DECISIONS.exists():
        return []
    with open(DECISIONS, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_decisions(rows: list[dict]) -> None:
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with open(DECISIONS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DECISION_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["item"], r["decision"], r["name"])))


def cmd_review() -> None:
    catalogue = load_catalogue()
    with open(LIST, encoding="utf-8") as f:
        items = [r for r in csv.DictReader(f) if (r.get("item") or "").strip()]
    decisions = load_decisions()
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
            continue
        for i, r in enumerate(new, 1):
            decisions.append({"item": item, "product_id": r["product_id"], "name": r["name"], "url": r["url"],
                              "decision": "rejected" if i in rejected else "accepted"})
        save_decisions(decisions)
        print(f"   saved: {len(new) - len(rejected)} accepted, {len(rejected)} rejected")
    total = sum(1 for d in decisions if d["decision"] == "accepted")
    print(f"\n{total} accepted products in {DECISIONS} (= product pages fetched per weekly price run)")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "build":
        cmd_build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        cmd_search(" ".join(sys.argv[2:]))
    elif len(sys.argv) == 2 and sys.argv[1] == "review":
        cmd_review()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()