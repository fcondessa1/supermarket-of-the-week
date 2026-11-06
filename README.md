# Supermarket of the Week

Tells me where to do this week's shopping. For each item on my list ("leite meio gordo", "atum ao natural") it compares every brand and pack size by price per kg or litre, across supermarkets, and recommends one store, or a split across two when the saving is worth the extra trip.

I built it because I use it. It is also a portfolio project: the parts that matter are measured against data I checked by hand, and the failures are documented next to the fixes.

> **Status (2026-11-05):** Working end to end for Continente, Pingo Doce and Lidl: prices for every product that matches my list, compared per kg/L/unit across brands and stores, and a weekly recommendation (one store, or a split across two if it beats the trip cost). A GitHub Action runs it every Tuesday, commits the prices and posts the report as an issue. An LLM judges whether a product is what I need, scored at 93% on a held-out store (word rules: 90%). It now checks Lidl's offers in the weekly run, where nobody reviews matches. Dropped: reading the leaflets with a vision model (see below).

## How it works

A list item is a **need**, not a product. "leite meio gordo" means "whichever brand and pack size is cheapest per litre this week".

```
my list (plain words) ──► every matching product, all brands ──► price per kg / L / unit ──► cheapest per item
                          (reviewed once by me)                                                    │
weekly report ◄── one store, or two if worth the trip ◄── same items in each store ◄──────────────┘
```

Where the prices come from depends on what each store publishes:

| Store | Source | Why | Status |
|---|---|---|---|
| Continente | Continente Online product pages | Gives the current price, the price before discount, the promotion's end date and pack size for every product, not only promoted ones | Working |
| Lidl | lidl.pt product pages for this week's offers; regular prices from lidl.pt's "price before offer" in past weeks, plus a small table I keep by hand | lidl.pt lists only offers (248 products on 27 Oct 2026), not the regular range: no basmati rice, although Lidl sells it | Working |
| Pingo Doce | Pingo Doce Online product pages | Same platform as Continente, so the catalogue, review and weekly run are shared; only reading a page differs. `robots.txt` allows product pages | Working since 3 Nov 2026 |
| Intermarché | — | `robots.txt` disallows automated access | Not used |

**Known limitation:** Lidl doesn't publish regular prices online. Two substitutes, both labelled in the report and flagged after 60 days:

- **lidl.pt's own price before the offer.** Every weekly file is kept, and each offer shows the price without it: the crossed-out price, or the shelf price when the offer needs Lidl Plus. The latest one seen per product is reused after the offer ends, so the coverage grows every week. A shelf price on its own isn't used, since it may itself be the offer price. Some offers are limited-time products, so a price before offer doesn't prove the product is still sold.
- **My own table**, `data/shopping/lidl_regular.csv`, for what lidl.pt never shows, filled in from the shelf or my Lidl Plus receipts.

All of them are compared, and the cheapest per kg/L/unit wins.

## The weekly recommendation

`src/recommend.py` takes the latest price file of each store and, for every item on my list:

1. finds the cheapest product per kg / L / unit at each store, on one basis across both stores. Lidl never states a drained weight, so tuna is compared on total weight everywhere.
2. costs it as unit price × my quantity. Pack rounding is ignored: 0.5 kg of granola may mean a 375 g pack.

Then it compares three plans, all costed on the same items: everything at Continente, everything at Lidl (each only if that store has a price for all of them), and each item where it's cheapest plus a trip cost (3 € by default) for going to two stores. Items with no price at either store are listed, not silently dropped. An earlier version recommended a split whenever neither store had every item, which on test data meant a trip to Lidl to save 0.42 € on milk; that is fixed.

From the first run, 27 October 2026, before I had recorded any Lidl regular prices:

| Item | Continente: cheapest | Lidl: cheapest |
|---|---|---|
| Eggs, class M | 0.25 €/egg | **0.20 €/egg** (30-pack, offer) |
| Canned tuna, total weight | 7.12 €/kg | **6.11 €/kg** (4-pack, Lidl Plus offer) |
| Lactose-free milk, basmati, granola, coffee capsules, blueberries | — | not on lidl.pt |

Lidl's two offers saved 1.07 € on my quantities, below the trip cost, so that week's answer was **Continente only**. That run is also what showed the gap: without Lidl's regular prices, the comparison was biased towards Continente, which is why the regular-price table exists.

## Matching with an LLM

Word rules find candidates: every product whose name has all of an item's words. They also let through things I don't mean: frozen blueberries and cranberries ("mirtilos vermelhos") for "mirtilo", granola bites and a vegan "chicken" for granola and chicken breast. For Continente and Pingo Doce I review matches once; Lidl's offers change every week and are never reviewed. So the question is whether a model can judge "is this what I need?" well enough to trust where I don't look.

**Set-up** (`src/llm_match.py`). For each list item, every catalogue product with the item's words is a test case, labelled from my own decisions: accepted = match; rejected in review, or removed by one of my exclusions ("achocolatado", "salada") = no match. The model sees the item and a one-line rule I wrote for it ("fresh blueberries only; not frozen; not freeze-dried; not cranberries"), never my exclusions, and returns a JSON verdict and a reason per product. Continente's decisions were used to develop the prompt; Pingo Doce's were scored once, with the prompt frozen and a pass mark set beforehand. Labels were redone and committed before the first model run on them.

**Prompt versions, on the Continente tuning set (Claude Haiku 5.5):**

| Version | Change | Right | Wrong products caught | Good products wrongly rejected |
|---|---|---|---|---|
| v1 | Item and my exclusions | 276 of 285 | — (no wrong products in the test) | 9 |
| v2 | Exclusions no longer given, so they become test cases | 278 of 287 (97%) | 28 of 32 | 5 |
| v3 | Named the kinds of mistake (snack forms, ready meals, frozen) and made my notes binding | 247 of 287 (86%) | 35 of 38 | **37** |
| v4 | Rules in their own column; pack size never matters; no guessing what a brand sells | **281 of 287 (98%)** | 35 of 38 | 3 |

Between v2 and v3 I re-checked the labels the model disagreed with, and changed several (net: 255 → 249 products labelled a match), e.g. a ready meal and a vegan imitation I had accepted as chicken breast. v2's 97% is on the old labels.

**Held-out result, Pingo Doce, prompt v4, scored once:**

| | Right | Wrong products caught | Good products wrongly rejected | Cost |
|---|---|---|---|---|
| Word rules alone | 89 of 106 (84%) | 0 of 17 | 0 | — |
| Word rules + my exclusions | 95 of 106 (90%) | 6 of 17 | 0 | — |
| Claude Haiku 5.5 | **99 of 106 (93%)** | **16 of 17** | 6 of 89 | $0.0033 |
| Claude Sonnet 5.5 | 99 of 106 (93%) | 16 of 17 | 6 of 89 | $0.052 |

Both models disagreed with me on the same 7 products, and none of the 7 is a model mistake against what I had written down: five egg packs graded "classe M/L" (my item says "classe m"; I had accepted them), a coffee granola I accepted although my own rule excludes coffee, and fresh milk I rejected under a "UHT only" rule I never wrote. So the remaining errors are in my specification, and the larger model buys nothing here: Haiku, 16 times cheaper, is the one to use. (18 of the 106 answers were reused from the Continente run, where the same product name appeared for the same item; the cache key is the model, prompt, item, rule and name, and contains no labels.)

**What went wrong on the way:**
- **v1 couldn't measure what mattered.** Giving the model my exclusions meant every wrong product had already been removed: 285 cases, none of them wrong. Exclusion-removed products became the negative cases from v2.
- **v3 got worse, and the test caught it.** My notes mixed rules with quantities ("12 eggs", "two 125 g punnets"); told to treat notes as rules, the model rejected 32 of 33 egg packs for not showing "12". Rules now have their own column, and notes are never sent.
- **Invented facts.** v3 said brands can be a clue; the model then called Sekoya "a frozen-fruit brand", which I don't believe is true. v4 judges from the name only, at the cost of missing one frozen brand (Gelcampo) that the name doesn't reveal.
- **My labels contradicted my rules.** A freeze-dried blueberry product and a coffee granola were accepted against rules I had written. The model applied the rules more consistently than I did.

## Continente: how an item becomes a ranked list

1. **Catalogue.** Continente publishes product sitemaps for search engines. Each entry's address holds the product's name and ID, e.g. `.../produto/atum-posta-ao-natural-continente-equilibrio-3697795.html`, so the whole catalogue costs 7 requests. It has no prices.
2. **Review, once per item.** Every product whose name contains all the item's words, minus phrases I exclude ("sem lactose", "achocolatado"), is shown to me once. I reject what isn't what I mean. Decisions are saved, and later reviews show only new products.
3. **Weekly prices.** For each accepted product, the product page gives:
   - the price and promotion end date (JSON-LD block)
   - the price before discount, whether it's sold by weight, and its category (the page's analytics data)
   - the pack size ("emb. 6 x 1 lt")
   - Continente's own €/kg

   The script computes €/kg, €/L or €/unit itself and ranks each item.

Example from 22 October 2026:

```
== arroz agulha: 14 of 22 products priced per kg
      1.08 €/kg   5.39 € Arroz Agulha Pack Poupança Continente
      1.15 €/kg   1.15 € Arroz Agulha Continente
      1.19 €/kg   2.38 € Arroz Agulha Oriente
```

Why not fetch the price of every catalogue product: that would be one ~1.6 MB page per product every week, hours of requests for things I never buy. Only accepted products are fetched, one per second.

## Evaluation

| What is measured | Against | Result |
|---|---|---|
| Unit price (normalisation): my €/kg or €/L vs Continente's own printed figure | 114 Continente products for 4 items, 22 Oct 2026 | Before fix: 28 agree, 18 differ. After fix: **42 agree, 4 differ**; 22 have no figure to compare (see below) |
| Same check, larger list | 153 Continente products for 7 items, 27 Oct 2026 | **123 agree, 4 differ** (the known tuna cases); 14 are 1 kg/1 L packs, 12 not checkable |
| Same check, Pingo Doce | 97 Pingo Doce products for 7 items, 3 Nov 2026 | **64 agree, 0 differ**; 13 not checkable (no €/kg on the page); 20 without a price |
| Page filter v1: recall and precision | Hand labels, all 43 pages of the Lidl leaflet of 19–25 Oct 2026 (dev set) | Recall 0.97 (29 of 30 grocery pages kept), precision 0.88; 10 of 43 pages skipped |
| Page filter v2 | Hand labels, Lidl leaflet of 12–18 Oct 2026 (held-out test set, 40 pages) | Recall 0.96 (27 of 28), precision 0.90; 10 of 40 pages skipped |
| Page filter v3 | — | Not scored: leaflet extraction dropped |
| Product matching: LLM (Haiku 5.5, prompt v4) vs word rules | My decisions on 106 Pingo Doce products (held-out) | **93% right vs 90%**; 16 of 17 wrong products caught; 6 of 89 good ones rejected (see *Matching with an LLM*) |

### Unit prices: what went wrong and what changed

- **Drained weight.** 17 of the first 18 disagreements were canned tuna. Continente computes €/kg on the drained weight ("peso escorrido"), while I had used the total weight: 2,74 € ÷ 0,270 kg = 10,15 €/kg is Continente's figure. Drained weight is also the fair comparison (you pay for the fish, not the water), so the script now uses it whenever it is printed.
- **Continente disagrees with itself.** Three tuna products still differ, because Continente's €/kg implies a different drained weight than its own description. Minerva says "peso escorrido 78 gr", but 1,29 € ÷ 15,36 €/kg = 84 g. My figure follows the stated weight, and the price file records what the page's figure implies.
- **A wrong product, caught by the check.** The fourth disagreement was a tuna-and-egg salad that I had accepted under "atum ao natural" during review.
- **22 not checkable.** The page shows no per-kg figure. The ones I inspected were all 1 kg packs, where the unit price is the price; these are now labelled as such.
- **A parser bug.** The first version looked for the product's analytics entry by its discount field, so it missed every product without a discount (86 of 114 rows had a note).

### Pingo Doce: what went wrong and what changed

- **A price of 0 is not a price.** In the first run, most products had `"price": 0` in the page's analytics data, and the script took it at face value: "everything at Pingo Doce for 0.00 €". Zero now means no price, and unit prices of 0 are ignored for every store. 20 of 97 products had no price; why (out of stock, or not sold online) is not yet checked.
- **Weights written as counts.** Pingo Doce gives some pack sizes as "1.5 Un" (a 1.5 kg bag of rice) or "0.084 Un" (84 g of tuna). A count with decimals is treated as unknown: the page's own €/kg is used if it prints one, otherwise the product is left out, rather than guessing kg (which would be wrong for anything sold by the litre).
- **Matching shows its limits.** Words alone accept frozen blueberries and cranberries ("mirtilos vermelhos") for "mirtilo". The review catches them; the LLM step is meant to catch them for offers that are never reviewed.

### Products sold by weight: what went wrong and what changed

Fresh chicken and loose sweet potato are priced **per kg** at both shops, with only an approximate pack weight. The first version treated that price as a pack price and divided it by the weight again:

| Product | Page says | v1 computed | Now |
|---|---|---|---|
| Peito de Frango em Vácuo, Continente | "6,49€/kg 14,28€/un", 2,2 kg (aprox.) | 2.95 €/kg | 6.49 €/kg |
| Batata Doce, Pingo Doce | "2,49 €/Kg 1 Un 0,37 kg" | 6.73 €/kg | 2.49 €/kg |
| Peito de Frango Embalado, Pingo Doce | "6,59 €/Kg 1 Un 0,6 kg" | 10.98 €/kg | 6.59 €/kg |

The unit-price check couldn't catch it: these pages print no separate €/kg to compare against, because the price already *is* the €/kg. The chicken made it into a recommendation before I noticed it was a third of the usual price. The pages now decide: Continente's "X €/kg Y €/un" and Pingo Doce's "X €/Kg" in the price block, where X is the product's price, mean a per-kg price. Two of my own fixes failed on the way: the first Pingo Doce rule looked for "Peso aproximado", which the sweet potato page doesn't say, and the first Continente rule didn't decode `&euro;` in the raw page.

### Products the catalogue lists but Continente doesn't sell

46 of the 114 accepted products (40%) redirect to Continente's home page: they are still in the search-engine catalogue but not sold online. They are marked unavailable and skipped in weekly runs; a monthly `--recheck` puts them back if they return.

### Page filter (Lidl): what went wrong and what changed

Lidl leaflets have many non-food pages, so a free keyword filter on the leaflet's own page descriptions skips them before any model call. Recall matters more than precision: a missed page means missed offers, an extra page costs one model call.

- **v1** missed a page of German specialities (no rule for sausages or herring). Three of its four extra pages matched boilerplate in Lidl's keyword field ("fresco", "água"). The fourth was flowers: "molhos de rosas" matched "molho" (sauce).
- **v2** reads only the page description, adds the missing food words, and ignores flower bunches. These changes came from the dev set's errors, so it was scored on a different leaflet. On the dev leaflet it newly missed a Halloween sweets page that v1 had caught through the keywords, a cost my spot check didn't show.
- **v3** fixes a general bug: short stems matched longer words ("bolo" matched "bolor", "nata" would match "Natal"). It also adds sweets and drops pet food, which isn't on my list. Both leaflets shaped it, so it is scored on the next unseen one.
- **Left in on purpose.** Three errors would need rules for single phrases: a charity page whose description omits its products, a pet "bolsa de snacks", and "produtos de limpeza Dirt Devil" (gadgets).

## Why the leaflets aren't read by a model

The first plan was to read Lidl's weekly leaflet with a vision model and score it against hand labels and lidl.pt. I dropped it: lidl.pt already gives this week's offers as structured data, and Continente Online gives every product's price, so the leaflet would add only in-store-only offers, at an API cost per page and with a model error rate to manage. The leaflet downloaders and the page filter (scored above) stay in the repository as the groundwork, but the weekly run doesn't use them.

## Things the stores' data does that the code has to handle

- **Conditional prices.** "-20% em Lidl Plus" or a card price is not the shelf price. Lidl offers get a `condition` field.
- **Leaflet vs online prices.** Continente's leaflet warns that store prices and promotions may differ from Continente Online. This project uses online prices for Continente and says so.
- **Structured leaflet data is not the leaflet price.** The product hotspots in Continente's leaflet carry the price *before* the promotion (Pescanova Supremas: hotspot 11.28, page text "PVPR 11,28 € · 8,99 €").
- **Promotion end dates.** Some products show a far-off "valid until" (e.g. 2027-01-01). Anything more than 45 days away is shown as "no end date".
- **Different weeks per store.** Lidl's offers run Monday to Sunday; Continente's run Tuesday to Monday.

## Cost

| Step | API cost | Load on the store's site |
|---|---|---|
| Continente catalogue | €0 | 7 requests, when rebuilt |
| Continente weekly prices | €0 | one request per accepted product (68 this week), one per second |
| Pingo Doce weekly prices | €0 | one request per accepted product (97), one per second |
| Leaflet downloads (not part of the weekly run) | €0 | one leaflet per store, when run |
| Page filter | €0 | none (keyword rules) |
| Weekly GitHub Action | €0 (free minutes, about 20 per run) | the three weekly price runs above |
| LLM matching evaluation | $0.088 in total for all runs below (Haiku 5.5: under 1 cent per run) | none |

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

# Continente: catalogue (rebuild occasionally), review new products for my list, this week's prices
python src/continente_catalogue.py build
python src/continente_catalogue.py review
python src/fetch_continente_online.py            # add --recheck about once a month

# Pingo Doce: the same steps
python src/catalogue.py pingodoce build
python src/catalogue.py pingodoce review
python src/fetch_pingodoce_online.py

# Lidl: this week's offers from lidl.pt
python src/fetch_lidl_online.py

# LLM matching: score against my decisions (Continente = tuning set, Pingo Doce = held-out)
python src/llm_match.py eval continente --dry-run     # case count and cost estimate, no API calls
python src/llm_match.py eval pingodoce --model haiku  # needs ANTHROPIC_API_KEY

# Where to shop (writes data/reports/<date>.md)
python src/recommend.py                          # --trip-cost 5, --no-lidl-plus

# Leaflets
python src/fetch_lidl.py
python src/fetch_continente.py

# Page filter (Lidl): label a leaflet's pages by hand, then predict and score
FLYER=data/raw/lidl/<flyer id>
python src/classify_pages.py template $FLYER
python src/classify_pages.py auto $FLYER
python src/classify_pages.py eval $FLYER
```

## Repository layout

```
.github/workflows/weekly.yml    every Tuesday: prices, recommendation, commit, report as an issue
src/catalogue.py                catalogue from a store's sitemaps (continente, pingodoce); review which products count as each item
src/continente_catalogue.py     the same for Continente (older commands)
src/fetch_pingodoce_online.py   weekly Pingo Doce prices: reads its product pages, reuses Continente's run
src/fetch_continente_online.py  weekly prices, unit prices and checks for accepted products
src/fetch_lidl_online.py        this week's Lidl offers from lidl.pt (decodes the pages' Nuxt data)
src/recommend.py                cheapest store per item, plans, and the recommendation
src/llm_match.py                LLM judgement of product matches, evaluated against my decisions
data/llm/                       cached model answers and one evaluation file per store, model and prompt version
data/shopping/lidl_regular.csv  Lidl regular prices I record by hand (lidl.pt doesn't publish them)
data/prices/lidl/               one Lidl price file per run (committed)
data/reports/                   one recommendation per run (committed)
src/fetch_lidl.py               download Lidl leaflet metadata and page images
src/fetch_continente.py         download Continente leaflet page text, product hotspots and page images
src/classify_pages.py           page filter: label template, keyword-rule predictions, evaluation
src/make_offer_labels.py        blank offer-label files per leaflet page, and a format check
data/shopping/list.csv          my list: item (plain words; ^ = first word anchored), quantity, unit, exclude, notes
data/shopping/<store>_products.csv      my decisions: which products count as each item
data/prices/continente/         one price file per run (committed: the price history)
data/labels/                    my hand labels and the predictions they are scored against
data/raw/, data/catalogue/      downloads (not committed; leaflet images are the stores' material)
```

## Responsible use

Personal, low-frequency use: one request per second, `robots.txt` respected (Continente's disallows search and category paging, which this project doesn't use), no leaflet images redistributed. Only public prices, my labels and metrics are published.
