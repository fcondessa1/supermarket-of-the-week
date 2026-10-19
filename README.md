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
| Page filter | Skip pages with nothing from a shopping list (tools, clothes, plants) before any API call | Done: ~25–33% of Lidl pages skipped; v2 held-out recall 0.96 |
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
| Continente | iPaper flipbook viewer. The viewer page embeds a config with each page's printed text, signed links to the page images, and "enrichment" files with the product behind each cart icon (Continente product ID, name, price) | `src/fetch_continente.py` (automatic: finds the week's leaflet on the listing page and skips the Continente Bom Dia and Madeira editions) |
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
| Page filter v1: recall (grocery pages kept) and precision | Hand labels for all 43 pages of the Lidl leaflet of 19–25 Oct 2026 (dev set) | Recall 0.97 (29 of 30 grocery pages kept), precision 0.88 (4 extra pages); 10 of 43 pages skipped |
| Page filter v2 (changed after reading v1's errors) | Hand labels for a different Lidl leaflet (12–18 Oct 2026, held-out test set, 40 pages) | Recall 0.96 (27 of 28), precision 0.90 (3 extra pages); 10 of 40 pages skipped |
| Page filter v3 (changed after reading v2's errors on both leaflets) | Next unseen Lidl leaflet (26 Oct–1 Nov 2026) | Pending |
| Extraction: field-level accuracy (price, pack size, condition) | 15–20 hand-labelled pages across the three stores | Pending |
| Normalisation: computed unit price vs the unit price printed on the leaflet | All extracted offers that print one | Pending |
| Matching: precision and recall | Hand-labelled matches for my list over 2–3 weeks | Pending |

For the page filter, recall matters more than precision: a missed page means missed offers, while an extra page only costs one API call.

**What v1 got wrong and what changed.** v1 missed one page of German specialities (sausages, marinated herring) because no rule covered them. Three of its four extra pages matched in Lidl's keyword field, which mixes product names with boilerplate and legal text ("fresco", "água"). The fourth was a flowers page: "molhos de rosas" (bunches of roses) matched the stem for "molho" (sauce). v2 reads only the page description, adds the missing food stems, removes flower bunches before matching, and only counts "produtos de limpeza" rather than any page mentioning cleaning. These changes were made by looking at the dev set's errors, so v2's score on that same leaflet would be flattering. v2 is scored on a different leaflet instead.

**What v2 got wrong.** On the held-out leaflet it held up (recall 0.96, precision 0.90). But dropping the keyword field had a cost I didn't catch in my spot check: on the dev leaflet, v2 missed a Halloween sweets page that v1 had found through the keywords. Its other errors exposed a general bug. Short stems also matched longer, unrelated words: "bolo" (cake) matched "bolor" (mould), and "nata" (cream) would match "Natal" (Christmas). v3 matches stems of four letters or fewer only as whole words or plurals, adds sweets vocabulary, and drops pet food, which isn't on my list (my labels mark pet-food pages 0). Both leaflets have now been used to shape the rules, so v3 gets scored on the next unseen leaflet.

**Errors left in on purpose.** Three pages in the two leaflets are still wrong, and fixing them would mean writing rules for single phrasings:
- A charity-voucher page I labelled relevant: its alt text describes only the campaign, not the products on it. That is a limit of the description, not of the rules.
- A pet-accessories page that mentions a "bolsa de snacks" (treat bag).
- A household page that mentions "produtos de limpeza Dirt Devil" (cleaning gadgets, not consumables).

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

# 1b. Download this week's mainland Continente leaflet (or pass a viewer address)
python src/fetch_continente.py

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

## Responsible use

Personal, low-frequency use: one download per leaflet per week, one request per second, `robots.txt` respected, no leaflet images redistributed. Only extracted offer data, labels and metrics are published.
