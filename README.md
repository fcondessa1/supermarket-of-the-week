# Supermarket of the Week

Tells me where to do this week's shopping. For each item on my list ("leite meio gordo", "atum ao natural") it compares every brand and pack size by price per kg or litre, across supermarkets, and recommends one store, or a split across two when the saving is worth the extra trip.

I built it because I use it. It is also a portfolio project: the parts that matter are measured against data I checked by hand, and the failures are documented next to the fixes.

> **Status (2026-10-27, week 3 of 3):** Working end to end for Continente and Lidl: prices for every product that matches my list, compared per kg/L/unit across brands and stores, and a weekly recommendation (one store, or a split if it beats the trip cost). Not built yet: the scheduled weekly run, reading Lidl's leaflet with a vision model (scored against lidl.pt), Pingo Doce.

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
| Lidl | lidl.pt product pages for this week's offers; regular prices from lidl.pt's "price before offer" in past weeks, plus a small table I keep by hand | lidl.pt lists only offers (248 products on 27 Oct 2026), not the regular range: no lactose-free milk or basmati rice, although Lidl sells both. The leaflet will be read by a vision model and scored against lidl.pt's prices | Offers working; leaflet extraction next |
| Pingo Doce | Online shop, if it allows automated requests | The website blocked my first automated requests | Not started |
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
| Page filter v1: recall and precision | Hand labels, all 43 pages of the Lidl leaflet of 19–25 Oct 2026 (dev set) | Recall 0.97 (29 of 30 grocery pages kept), precision 0.88; 10 of 43 pages skipped |
| Page filter v2 | Hand labels, Lidl leaflet of 12–18 Oct 2026 (held-out test set, 40 pages) | Recall 0.96 (27 of 28), precision 0.90; 10 of 40 pages skipped |
| Page filter v3 | Next unseen Lidl leaflet (26 Oct–1 Nov 2026) | Pending |
| Lidl extraction: offers found and field accuracy (price, pack size, condition) | Hand-labelled Lidl pages | Pending |
| Matching Lidl offers to list items | Hand-labelled matches | Pending |

### Unit prices: what went wrong and what changed

- **Drained weight.** 17 of the first 18 disagreements were canned tuna. Continente computes €/kg on the drained weight ("peso escorrido"), while I had used the total weight: 2,74 € ÷ 0,270 kg = 10,15 €/kg is Continente's figure. Drained weight is also the fair comparison (you pay for the fish, not the water), so the script now uses it whenever it is printed.
- **Continente disagrees with itself.** Three tuna products still differ, because Continente's €/kg implies a different drained weight than its own description. Minerva says "peso escorrido 78 gr", but 1,29 € ÷ 15,36 €/kg = 84 g. My figure follows the stated weight, and the price file records what the page's figure implies.
- **A wrong product, caught by the check.** The fourth disagreement was a tuna-and-egg salad that I had accepted under "atum ao natural" during review.
- **22 not checkable.** The page shows no per-kg figure. The ones I inspected were all 1 kg packs, where the unit price is the price; these are now labelled as such.
- **A parser bug.** The first version looked for the product's analytics entry by its discount field, so it missed every product without a discount (86 of 114 rows had a note).

### Products the catalogue lists but Continente doesn't sell

46 of the 114 accepted products (40%) redirect to Continente's home page: they are still in the search-engine catalogue but not sold online. They are marked unavailable and skipped in weekly runs; a monthly `--recheck` puts them back if they return.

### Page filter (Lidl): what went wrong and what changed

Lidl leaflets have many non-food pages, so a free keyword filter on the leaflet's own page descriptions skips them before any model call. Recall matters more than precision: a missed page means missed offers, an extra page costs one model call.

- **v1** missed a page of German specialities (no rule for sausages or herring). Three of its four extra pages matched boilerplate in Lidl's keyword field ("fresco", "água"). The fourth was flowers: "molhos de rosas" matched "molho" (sauce).
- **v2** reads only the page description, adds the missing food words, and ignores flower bunches. These changes came from the dev set's errors, so it was scored on a different leaflet. On the dev leaflet it newly missed a Halloween sweets page that v1 had caught through the keywords, a cost my spot check didn't show.
- **v3** fixes a general bug: short stems matched longer words ("bolo" matched "bolor", "nata" would match "Natal"). It also adds sweets and drops pet food, which isn't on my list. Both leaflets shaped it, so it is scored on the next unseen one.
- **Left in on purpose.** Three errors would need rules for single phrases: a charity page whose description omits its products, a pet "bolsa de snacks", and "produtos de limpeza Dirt Devil" (gadgets).

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
| Leaflet downloads | €0 | one leaflet per store per week |
| Page filter | €0 | none (keyword rules) |
| Lidl extraction | To be measured per page | none |

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

# Continente: catalogue (rebuild occasionally), review new products for my list, this week's prices
python src/continente_catalogue.py build
python src/continente_catalogue.py review
python src/fetch_continente_online.py            # add --recheck about once a month

# Lidl: this week's offers from lidl.pt
python src/fetch_lidl_online.py

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
src/continente_catalogue.py     catalogue from Continente's sitemaps; review which products count as each item
src/fetch_continente_online.py  weekly prices, unit prices and checks for accepted products
src/fetch_lidl_online.py        this week's Lidl offers from lidl.pt (decodes the pages' Nuxt data)
src/recommend.py                cheapest store per item, plans, and the recommendation
data/shopping/lidl_regular.csv  Lidl regular prices I record by hand (lidl.pt doesn't publish them)
data/prices/lidl/               one Lidl price file per run (committed)
data/reports/                   one recommendation per run (committed)
src/fetch_lidl.py               download Lidl leaflet metadata and page images
src/fetch_continente.py         download Continente leaflet page text, product hotspots and page images
src/classify_pages.py           page filter: label template, keyword-rule predictions, evaluation
src/make_offer_labels.py        blank offer-label files per leaflet page, and a format check
data/shopping/list.csv          my list: item (plain words; ^ = first word anchored), quantity, unit, exclude, notes
data/shopping/continente_products.csv   my decisions: which products count as each item
data/prices/continente/         one price file per run (committed: the price history)
data/labels/                    my hand labels and the predictions they are scored against
data/raw/, data/catalogue/      downloads (not committed; leaflet images are the stores' material)
```

## Responsible use

Personal, low-frequency use: one request per second, `robots.txt` respected (Continente's disallows search and category paging, which this project doesn't use), no leaflet images redistributed. Only public prices, my labels and metrics are published.
