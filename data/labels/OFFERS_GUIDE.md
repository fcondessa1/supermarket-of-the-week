# Labelling guide: offers

One CSV per leaflet page, one row per **offer as printed**: one price box = one row. Label from the page image; the page text is only there to copy names from.

Label a page completely, before running the model on it. A half-labelled page makes recall meaningless.

## What counts as an offer

- A product (or group of products) with a **concrete price** printed on the page.
- One price for several varieties ("vários sabores", "preta/rosada"): **one row**. Name it as printed and write `varieties` in `notes`.
- The same offer printed twice on one page: one row.
- **Not labelled:** category-wide discounts without a price ("Até 30% desconto no red fish"), recipes, ads, non-grocery items.

## Columns

| Column | What to write | Examples |
|---|---|---|
| `offer` | 1, 2, 3… in reading order (top-left to bottom-right) | `1` |
| `name` | Product name as printed, without the brand if the brand is printed separately | `Supremas de bacalhau congelado` |
| `brand` | As printed; `Continente`, `Lidl` brands such as `Milbona` count; blank if none | `Pescanova` |
| `size` | Pack size as printed; blank for loose products sold per kg | `400 g`, `6 x 33 cl`, `1 kg` |
| `price` | The price you pay as advertised, dot as decimal separator | `8.99` |
| `price_per` | What `price` refers to: `unit` (one pack/item), `kg`, or `l` | `unit` |
| `regular_price` | Crossed-out, "PVPR" or "antes" price, if printed | `11.28` |
| `unit_price` | The small printed €/kg, €/L or €/unit, if printed | `22.48` |
| `unit_price_per` | `kg`, `l` or `unit`, if `unit_price` is filled | `kg` |
| `condition` | What you must do to get `price`: `none`, `card` (Cartão Continente, Poupa Mais), `app` (Lidl Plus), `quantity` (must buy N, e.g. "leve 2 pague 1"), `other` | `none` |
| `discount_text` | The discount badge as printed, if any | `20% sobre PVPR` |
| `notes` | Anything odd: `varieties`, unreadable digits, unsure | `varieties` |

## Decisions for tricky cases

- **"Desconto Direto"** (Continente): the price shown is what you pay at the till, so `condition` is `none` unless the box says a card is needed.
- **Lidl Plus**: if the big price needs the app, `price` is the app price, `condition` is `app`, and the non-app price (if printed) goes in `regular_price`.
- **Multibuy** ("leve 2 pague 1", "2.ª unidade -50%"): `price` is the price of one unit as printed, `condition` is `quantity`, and the deal goes in `discount_text`.
- **Price per kg for a packed product** ("8,99 €/KG", sold by weight): `price` = 8.99, `price_per` = `kg`, `size` blank.
- If you can't read a digit, write your best guess and `unreadable` in `notes`.
