# Supermarket of the Week

Reads the weekly promotion leaflets (*folhetos*) of Portuguese supermarkets, matches the offers against my shopping list, and tells me where to shop this week: one store, or a split across two when the saving is worth the extra trip.

I built it because I use it. It is also a portfolio project: the parts that matter are measured against data I labelled by hand, and the failures are documented next to the fixes.

> **Status (week 1 of 3, started 2026-10-12):** Lidl and Continente leaflets are downloaded automatically, and a zero-cost page filter is written. The Pingo Doce fetcher, extraction, matching and the shopping recommendation are not built yet.

## How it works (planned v1)

```
fetch leaflets ──► filter grocery pages ──► extract offers (vision model) ──► normalise to €/kg, €/L, €/unit
                                                                                        │
weekly report ◄── choose store or pair of stores ◄── match offers to my shopping list ◄─┘
```

| Stage | What it does | Status |
|---|---|---|
| Fetch | Download each store's weekly leaflet pages and metadata | Lidl and Continente done; Pingo Doce in progress |
| Page filter | Skip pages with nothing from a shopping list (tools, clothes, plants) before any API call | Written, eval pending |
| Extraction | Vision model turns each page into JSON offers: name, brand, pack size, price, condition, dates | Not started |
| Normalisation | Deterministic code parses pack sizes ("emb. 4 x 160 gr") into price per kg/L/unit | Not started |
| Matching | Rules, then embeddings, then an LLM only for ambiguous cases; my confirmations are cached | Not started |
| Optimisation | Compare every single store and every pair, with a € penalty per extra trip | Not started |
| Report | Weekly GitHub issue, like my [Filing Diff](https://github.com/fcondessa1/Filing-Diff) project | Not started |

**Known limitation, by design:** leaflets only list promoted items. Items on my list that are not on promotion are priced from a small baseline table that I maintain by hand. The report will say which prices came from the baseline.

## Sources

| Store | How the leaflet is published | How this project gets it |
|---|---|---|
| Lidl | Viewer backed by a public JSON endpoint: flyer id, offer dates, 1200 px page images, per-page alt text and keywords, plus a PDF | `src/fetch_lidl.py` (automatic) |
| Continente | iPaper flipbook viewer. The viewer page embeds a config with each page's printed text, signed links to the page images, and "enrichment" files with the product behind each cart icon (Continente product ID, name, price) | `src/fetch_continente.py` (needs the week's viewer address if the listing page doesn't expose it) |
| Pingo Doce | Website blocks automated requests | Being investigated; fallback is a 1-minute manual weekly download |
| Intermarché | `robots.txt` disallows automated access | Not used |

Things the leaflets do that the data model has to handle:

- **Conditional prices.** "-20% em Lidl Plus" or a Continente card price is not the shelf price. Every offer gets a `condition` field.
- **Repeated offers.** Lidl's cover repeats offers from inner pages. Lidl prints article numbers (e.g. Nº5912266), which are used to remove duplicates.
- **Leaflet vs online prices.** Continente's viewer itself warns that leaflet prices and promotions may differ from Continente Online, so online prices are not used as a substitute for leaflet prices.
- **Structured data is not the leaflet price.** Continente's product hotspots carry a price, but it is the price *before* the promotion. On the leaflet of 6–12 October 2026, Pescanova Supremas has hotspot price 11.28, while the page text reads "20 % Sobre PVPR · PVPR 11,28 € · 8,99 €". The hotspots are used for product IDs and names, and prices come from the page itself.
- **Different weeks per store.** Lidl's weekly offers run Monday to Sunday; Continente's run Tuesday to Monday.

## Evaluation

All labels are made by hand, before looking at the model's or the rules' output, so the predictions cannot anchor them.

| What is measured | Against | Result |
|---|---|---|
| Page filter: recall (grocery pages kept) and precision | Hand labels for every page of one Lidl leaflet | Pending |
| Extraction: field-level accuracy (price, pack size, condition) | 15–20 hand-labelled pages across the three stores | Pending |
| Normalisation: computed unit price vs the unit price printed on the leaflet | All extracted offers that print one | Pending |
| Matching: precision and recall | Hand-labelled matches for my list over 2–3 weeks | Pending |

For the page filter, recall matters more than precision: a missed page means missed offers, while an extra page only costs one API call.

## Cost

| Step | API cost |
|---|---|
| Fetching leaflets | €0 (no API calls) |
| Page filter | €0 (keyword rules on the leaflet's own alt text) |
| Extraction | To be measured per page and reported here |

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

# 1. Download this week's Lidl leaflet (or pass a leaflet name, e.g. promocoes-semanais-a-partir-de-12-10)
python src/fetch_lidl.py

# 1b. Download a Continente weekly leaflet (pass the viewer address if it isn't found automatically)
python src/fetch_continente.py https://folhetos.continente.pt/2026/10/semanal-NN-xxxx/

# 2. Page filter: make a blank label file, label it by hand, then predict and score
FLYER=$(ls -d data/raw/lidl/*/ | head -n 1)
python src/classify_pages.py template $FLYER   # fill 'relevant' with 1/0 by looking at each page
python src/classify_pages.py auto $FLYER
python src/classify_pages.py eval $FLYER
```

## Repository layout

```
src/fetch_lidl.py        download Lidl leaflet metadata and page images
src/fetch_continente.py  download Continente leaflet page text, product hotspots and page images
src/classify_pages.py    page filter: label template, keyword-rule predictions, evaluation
data/raw/                downloaded leaflets (NOT committed: the images are the stores' copyrighted material)
data/labels/             my hand labels and the predictions they are scored against (committed)
```

## Known issues

- The page filter barely helps on Continente: it keeps 50 of 52 pages of the 6–12 October leaflet, because the weekly leaflet is almost entirely groceries. Its savings are on Lidl, which has many non-food pages.
- 73 of the 161 Continente product hotspots on pages 1–11 have no position on the page (they appear to be entries in a "several flavours" menu), so they can't be tied to a specific price box by position alone.
- The page filter keeps a plants page because "molhos de rosas" (bunches of roses) matches the stem for "molho" (sauce). Left in on purpose until the full hand-labelled eval shows how much such errors matter.

## Responsible use

Personal, low-frequency use: one download per leaflet per week, one request per second, `robots.txt` respected, no leaflet images redistributed. Only extracted offer data, labels and metrics are published.
