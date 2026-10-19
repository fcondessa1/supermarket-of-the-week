"""Download one Continente weekly leaflet (iPaper viewer): page images, page text and product data.

Usage:
    python src/fetch_continente.py                     # find the current mainland "semanal" leaflet on continente.pt
    python src/fetch_continente.py --all-editions      # also Continente Bom Dia, Madeira, Açores editions
    python src/fetch_continente.py <viewer_url>        # e.g. https://folhetos.continente.pt/2026/10/semanal-42-xxxx/
    python src/fetch_continente.py <viewer_url> --no-images   # text + product data only

How it works:
    The viewer page embeds a JSON config (window.staticSettings) that contains
    - the page count and the printed text of every page (pageTexts),
    - a signed CDN policy for the page images (aws.policy, valid about a day),
    - signed links to the "enrichments" files: the product hotspots behind the cart icons,
      each with a Continente product ID, name and a price.
    Tokens are read fresh from the page on every run, so nothing expires in this script.

Output (per leaflet, folder named after iPaper's paper GUID):
    data/raw/continente/<guid>/flyer.json        name, url, validity text, page count, fetch time
    data/raw/continente/<guid>/manifest.csv      page, file, text (the page's printed text)
    data/raw/continente/<guid>/enrichments.json  raw enrichment records, all chunks merged
    data/raw/continente/<guid>/products.csv      one row per product hotspot: page, product_id, name, price, box
    data/raw/continente/<guid>/page-XX.jpg       page images (about 1143 x 1617 px)

The enrichment "price" is NOT always the leaflet price. On the leaflet of 6-12 Oct 2026, Pescanova
Supremas has enrichment price 11.28 and the page text says "PVPR 11,28 EUR ... 8,99 EUR": the
enrichment holds the price before the promotion. Leaflet prices come from the page text or the image.

No LLM calls: this step costs nothing. data/raw/ stays out of git.
"""

import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

LISTING = "https://www.continente.pt/folhetos/"
OUT = Path("data/raw/continente")
HEADERS = {"User-Agent": "Mozilla/5.0 (supermarket-of-the-week; personal project)"}
DELAY_S = 1.0
VIEWER_RE = re.compile(r"https://folhetos\.continente\.pt/\d{4}/\d{2}/semanal-[a-z0-9-]+/")
# Weekly leaflets that are NOT the mainland Continente edition, identified by a tag in the slug.
# Seen in week 42 (Oct 2026): semanal-42-cbd-fj87 (Continente Bom Dia) and semanal-42-mad-ia44 (Madeira).
# "aco"/"acores"/"az" are guesses for the Azores edition, not yet seen in a slug.
OTHER_EDITIONS = {"cbd": "Continente Bom Dia", "mad": "Madeira", "aco": "Açores", "acores": "Açores", "az": "Açores"}


def get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r


def discover_viewer_urls() -> list[str]:
    """Look for weekly-leaflet viewer links in the listing page's HTML (may be empty if the list is built by JavaScript)."""
    html = get(LISTING).text.replace("\\/", "/")
    return list(dict.fromkeys(VIEWER_RE.findall(html)))


def edition(url: str) -> str | None:
    """Return the edition name if the slug carries a regional/format tag, else None (= mainland edition)."""
    slug_parts = url.rstrip("/").split("/")[-1].split("-")   # e.g. ['semanal', '42', 'cbd', 'fj87']
    for part in slug_parts[2:-1]:
        if part in OTHER_EDITIONS:
            return OTHER_EDITIONS[part]
    return None


def parse_settings(html: str) -> dict:
    marker = "window.staticSettings ="
    i = html.find(marker)
    if i < 0:
        raise RuntimeError("window.staticSettings not found: the viewer page format has changed.")
    settings, _ = json.JSONDecoder().raw_decode(html[i + len(marker):].lstrip())
    return settings


def validity_text(page_texts: list[str]) -> str:
    m = re.search(r"Promoções e preços válidos[^.]*\.", " ".join(page_texts[:2]))
    return m.group(0) if m else ""


def flatten_enrichments(settings: dict) -> list[dict]:
    """Download every enrichment chunk and return all records with a 1-based 'page' added."""
    records = []
    for chunk, url in settings["enrichments"]["chunkUrls"].items():
        first, last = (int(x) for x in chunk.split("-")) if "-" in chunk else (int(chunk), int(chunk))
        data = get(url).json()
        items = data.get("enrichments", [])
        indexes = [e["pageIndex"] for e in items if "pageIndex" in e]
        # pageIndex was global (0-based) in the one chunk inspected; guard in case later chunks are chunk-relative.
        offset = 0 if not indexes or min(indexes) >= first - 1 else first - 1
        if offset:
            print(f"  note: chunk {chunk} uses chunk-relative pageIndex; adding {offset}")
        for e in items:
            e["page"] = e.get("pageIndex", 0) + offset + 1
            if not first <= e["page"] <= last:
                print(f"  warning: enrichment {e.get('id')} maps to page {e['page']}, outside chunk {chunk}")
            records.append(e)
        time.sleep(DELAY_S)
    return records


def write_products(records: list[dict], path: Path) -> int:
    products = [e for e in records if e.get("productId")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "product_id", "name", "price_in_hotspot", "x", "y", "width", "height"])
        for e in sorted(products, key=lambda e: (e["page"], e.get("y", 0), e.get("x", 0))):
            w.writerow([e["page"], e["productId"], e.get("name", ""), e.get("price", ""),
                        e.get("x"), e.get("y"), e.get("width"), e.get("height")])
    return len(products)


def fetch(viewer_url: str, images: bool = True) -> None:
    viewer_url = viewer_url.split("?")[0]
    settings = parse_settings(get(viewer_url).text)

    base = settings["aws"]["url"]                       # https://cdn.ipaper.io/iPaper/Papers/<guid>/
    guid = base.rstrip("/").split("/")[-1]
    pages = settings["pages"]
    texts = settings.get("pageTexts", [])
    folder = OUT / guid
    folder.mkdir(parents=True, exist_ok=True)

    meta = {
        "name": settings.get("name"),
        "viewer_url": viewer_url,
        "paper_guid": guid,
        "paper_id": settings.get("paperId"),
        "validity_text": validity_text(texts),
        "page_count": len(pages),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (folder / "flyer.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{meta['name']} | {meta['validity_text'] or 'validity not found in text'} | {len(pages)} pages")

    with open(folder / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "file", "text"])
        for n in pages:
            w.writerow([n, f"page-{n:02d}.jpg", texts[n - 1] if n - 1 < len(texts) else ""])

    records = flatten_enrichments(settings)
    (folder / "enrichments.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    n_products = write_products(records, folder / "products.csv")
    print(f"  {n_products} product hotspots across {len({e['page'] for e in records if e.get('productId')})} pages")

    if images:
        policy = settings["aws"]["policy"]
        new = 0
        for n in pages:
            path = folder / f"page-{n:02d}.jpg"
            if path.exists():
                continue
            path.write_bytes(get(f"{base}Pages/{n}/Zoom.jpg?{policy}").content)
            new += 1
            time.sleep(DELAY_S)
        print(f"  {new} new page images ({len(pages) - new} already there)")
    print(f"  saved to {folder}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    images = "--no-images" not in sys.argv
    if args:
        urls = args
    else:
        urls = []
        for url in discover_viewer_urls():
            other = edition(url)
            if other and "--all-editions" not in sys.argv:
                print(f"skipping {other} edition: {url}")
            else:
                urls.append(url)
    if not urls:
        sys.exit("No weekly leaflet link found in the listing HTML (it is probably built by JavaScript).\n"
                 "Open https://www.continente.pt/folhetos/, open the weekly leaflet, and pass its address:\n"
                 "  python src/fetch_continente.py https://folhetos.continente.pt/2026/10/semanal-NN-xxxx/")
    for url in urls:
        fetch(url, images=images)


if __name__ == "__main__":
    main()
