"""Download one week's Lidl Portugal leaflet: metadata + page images.

Usage:
    python src/fetch_lidl.py                 # find this week's "Promoções Semanais" leaflets and fetch them
    python src/fetch_lidl.py <slug>          # fetch one leaflet, e.g. promocoes-semanais-a-partir-de-12-10

Output (per leaflet):
    data/raw/lidl/<flyer_id>/flyer.json      full API response (dates, pages, alt text)
    data/raw/lidl/<flyer_id>/manifest.csv    one row per page: number, file, keywords, alt text
    data/raw/lidl/<flyer_id>/page-XX.jpg     page images (1200px), skipped if already downloaded

No LLM calls: this step costs nothing.
data/raw/ must stay out of git (leaflet images are Lidl's copyrighted material).
"""

import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

API = "https://endpoints.leaflets.schwarz/v4/flyer"
LISTING = "https://www.lidl.pt/c/folhetos/s10020672"
OUT = Path("data/raw/lidl")
HEADERS = {"User-Agent": "supermarket-of-the-week (personal project)"}
DELAY_S = 1.0  # be polite: one request per second


def discover_slugs() -> list[str]:
    """Return slugs of the weekly-promotion leaflets linked from the Lidl leaflet page."""
    html = requests.get(LISTING, headers=HEADERS, timeout=30).text
    slugs = re.findall(r"/l/pt/folhetos/([a-z0-9-]+)/ar/0", html)
    weekly = [s for s in dict.fromkeys(slugs) if s.startswith("promocoes-semanais")]
    return weekly


def fetch_flyer(slug: str) -> dict:
    r = requests.get(API, params={"flyer_identifier": slug, "region_id": 0}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"API said no for {slug}: {data.get('message')}")
    return data["flyer"]


def save_flyer(flyer: dict) -> None:
    folder = OUT / flyer["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "flyer.json").write_text(json.dumps(flyer, ensure_ascii=False, indent=2), encoding="utf-8")

    pages = flyer.get("pages", [])
    print(f"{flyer.get('name')} {flyer.get('title')} | offers {flyer.get('offerStartDate')} -> "
          f"{flyer.get('offerEndDate')} | {len(pages)} pages")

    rows, downloaded = [], 0
    for p in pages:
        number = int(p.get("number", len(rows) + 1))
        fname = f"page-{number:02d}.jpg"
        path = folder / fname
        if not path.exists():
            img = requests.get(p["image"], headers=HEADERS, timeout=60)
            img.raise_for_status()
            path.write_bytes(img.content)
            downloaded += 1
            time.sleep(DELAY_S)
        rows.append({
            "page": number,
            "file": fname,
            "keywords": "; ".join(p.get("keyWords") or []) if isinstance(p.get("keyWords"), list) else (p.get("keyWords") or ""),
            "alt_text": (p.get("altText") or "").replace("\n", " "),
        })

    with open(folder / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page", "file", "keywords", "alt_text"])
        w.writeheader()
        w.writerows(rows)
    print(f"  saved to {folder} ({downloaded} new images, {len(rows) - downloaded} already there)")


def main() -> None:
    slugs = sys.argv[1:] or discover_slugs()
    if not slugs:
        sys.exit("No weekly leaflet found on the listing page. Pass a slug by hand.")
    for slug in slugs:
        save_flyer(fetch_flyer(slug))
        time.sleep(DELAY_S)


if __name__ == "__main__":
    main()
