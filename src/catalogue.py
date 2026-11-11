"""Store catalogues: names, product IDs and URLs from the stores' sitemaps, and word matching on them.

Usage (store = continente or pingodoce):
    python src/catalogue.py pingodoce build                # download the catalogue (a few requests; weekly run)
    python src/catalogue.py pingodoce search leite meio    # look up products by words

Which products count for a list item is decided per person in src/lists.py (the model's judgement for their
rule, and their overrides in the app). Until 2026-11-11 I reviewed every product by hand here (`review`);
those decisions became my overrides.

Where the catalogues come from:
    The stores' product sitemaps for search engines. Both shops run on the same platform (Salesforce
    Commerce Cloud), and each entry's URL holds the product's name and ID:
      Continente  https://www.continente.pt/sitemap_index.xml (6 product sitemaps)
                  .../produto/atum-posta-ao-natural-continente-equilibrio-3697795.html
      Pingo Doce  https://www.pingodoce.pt/home/sitemap_index.xml (2 product sitemaps, checked 2026-11-03)
                  .../home/produtos/leite-e-bebidas-vegetais/leite/.../leite-uht-meio-gordo-pingo-doce-48150.html
    No prices: those come from the product pages of products that count for someone (fetch_*_online.py).

data/catalogue/ is committed (the app needs it). Each product's sitemap "lastmod" date is not kept: it changes
every week, which made the whole 16 MB file differ in every weekly commit. Cost: no LLM calls.
"""

import csv
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
STORE = "continente"          # set by main(); the paths below follow it


def catalogue_path(store: str | None = None) -> Path:
    return Path(f"data/catalogue/{store or STORE}.csv")


def script(store: str | None = None) -> str:
    return f"python src/catalogue.py {store or STORE}"


HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
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
        rows.append({"product_id": m.group(2), "name": unquote(m.group(1)).replace("-", " "), "url": loc.group(1)})
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
        w = csv.DictWriter(f, fieldnames=["product_id", "name", "url"])
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
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
