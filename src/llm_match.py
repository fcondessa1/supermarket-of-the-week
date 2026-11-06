"""LLM product matching: does this product count as my list item? Measured against my own review decisions.

The word rules (catalogue.py) find candidates: every product whose name has all the item's words and none of
its excluded phrases. They let through things I don't mean: "mirtilos congelados" and "mirtilos vermelhos"
(cranberries) for "mirtilo". For Continente and Pingo Doce I review those once; Lidl's offers change every
week and are never reviewed. This script asks a model to judge each candidate, and scores it against my
decisions, to find out whether it can be trusted where I don't review.

Usage:
    python src/llm_match.py eval continente --dry-run          # count cases and estimate the cost, no API calls
    python src/llm_match.py eval continente --model haiku      # dev set: tune the prompt here
    python src/llm_match.py eval pingodoce --model haiku       # held-out test set: score once, don't tune on it
    python src/llm_match.py lidl                               # in use: judge this week's Lidl candidates (Haiku)
    (--model sonnet for the larger model)

The model sees each item's words and its `rule` column in data/shopping/list.csv, never `notes`.
Test cases: for each item on data/shopping/list.csv, every catalogue product with the item's words (see
cases() for the labels: my review decisions, and my exclusions for products they removed before review).
The model is not told the exclusions. Baselines: the word rules alone (accept every case), and the word
rules with my exclusions.

Outputs:
    data/llm/cache.jsonl                       every model answer, keyed by model + prompt version + item + product
                                                (re-running costs nothing; committed with the results)
    data/llm/eval_<store>_<model>_<prompt>.csv  one row per case: my label, the model's answer, its reason
Needs ANTHROPIC_API_KEY in the environment (a Codespaces / Actions secret, never in a file).
Cost: Claude Haiku 5.5 $0.10 / $0.50 per million input / output tokens; Sonnet 5.5 $2 / $10 (pricing page,
2026-11-04). The script prints the measured cost of every run from the API's token counts.
"""

import csv
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from catalogue import candidates, catalogue_path, normalise, tokens

LIST = Path("data/shopping/list.csv")
CACHE = Path("data/llm/cache.jsonl")
OUT = Path("data/llm")
MODELS = {  # name -> (model id, $ per million input tokens, $ per million output tokens)
    "haiku": ("claude-haiku-5-5", 0.10, 0.50),
    "sonnet": ("claude-sonnet-5-5", 2.00, 10.00),
}
BATCH = 40                    # products per request
PROMPT_VERSION = "v4"         # change when the prompt changes, so old cached answers aren't reused

SYSTEM = """You help a shopper in Portugal compare supermarket prices. The shopper's list says what they need, \
in Portuguese, in plain words. A product counts as the item only if buying it would satisfy that need: any \
brand and any pack size count. It does not count if it is a different product that merely contains the words: \
a flavoured or processed version, a ready meal or a dish that includes it, a snack form (bars, bites, \
clusters sold as a snack), a drink made from it, a children's or infant version, a plant-based imitation, \
a different variety or form (e.g. frozen instead of fresh) when the item implies otherwise, or a different \
fruit with a similar name.
Pack size, count and quantity never matter: the shopper compares prices per kg, litre or unit.
When the shopper gives a rule for the item, it decides: follow every part of it, even where you would \
judge otherwise.
Product names come from shop catalogues and may be lowercase without accents. Judge from the name; don't \
guess what a brand sells.
Judge every product you are given. Keep each reason under 15 words, in English."""

# v1 (2026-11-04): exclusions in the prompt, so the test had no wrong products.
# v2 (2026-11-04): no exclusions; Continente 278/287, 28 of 32 wrong products caught, 5 good ones rejected.
#     Misses: granola bites (snack form), a frozen-food brand, infant milk, a ready meal, a vegan imitation.
# v3 (2026-11-05): names those kinds of product, and makes the shopper's notes a rule. WORSE: 247/287; 37 good
#     products rejected, because the notes also hold quantities ("12 eggs", "two 125 g punnets") and the
#     model applied them as rules. Its brand hint also produced an invented claim (Sekoya "frozen-fruit brand").
# v4 (2026-11-05): rules in their own column of list.csv (notes are never sent); pack size never matters;
#     no brand guessing.

SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "match": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "match", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}


# ---------- cases ----------

def load_list() -> list[dict]:
    with open(LIST, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("item") or "").strip()]


def cases(store: str) -> list[dict]:
    """Every catalogue product that has an item's words, labelled from my own decisions:
        accepted / unavailable in review         -> match
        rejected in review                       -> no match   (source "review")
        never reviewed: one of my exclusions     -> no match   (source "exclusion": my rule, not a
          removed it ("achocolatado", "salada")                product-by-product decision)
        never reviewed, not excluded             -> left out (no label)
    The model is NOT told my exclusions: the test is whether it understands what I need, including wrong
    products no exclusion anticipated (v1 of this test passed the exclusions and so had no wrong products:
    2026-11-04, 285 cases, 0 rejected)."""
    with open(Path(f"data/shopping/{store}_products.csv"), encoding="utf-8") as f:
        decided = {}
        for d in csv.DictReader(f):
            decided[(d["item"], d["product_id"])] = d["decision"]
    if not catalogue_path(store).exists():
        sys.exit(f"{catalogue_path(store)} not found. Run: python src/catalogue.py {store} build")
    with open(catalogue_path(store), encoding="utf-8") as f:
        catalogue = [r | {"_tokens": tokens(r["name"]), "_norm": normalise(r["name"])} for r in csv.DictReader(f)]
    out = []
    for it in load_list():
        item, exclude = it["item"].strip(), (it.get("exclude") or "").strip()
        bans = [normalise(x).strip() for x in exclude.split(";") if x.strip()]
        for r in candidates(catalogue, item, ""):
            decision = decided.get((item, r["product_id"]))
            if decision in ("accepted", "unavailable"):
                label, source = True, "review"
            elif decision == "rejected":
                label, source = False, "review"
            elif any(b in r["_norm"] for b in bans):
                label, source = False, "exclusion"
            else:
                continue
            out.append({"item": item, "exclude": "", "rule": (it.get("rule") or "").strip(),
                        "product_id": r["product_id"], "name": r["name"], "label": label, "source": source})
    return out


def describe(item: str, exclude: str, rule: str) -> str:
    text = f"The shopper needs: \"{item.lstrip('^').strip()}\""
    if item.startswith("^"):
        text += f" (the product is a \"{tokens(item.lstrip('^'))[0]}\" itself, not something containing it)"
    if rule:
        text += f". Their rule for this item: {rule}"
    if exclude:
        text += f". Already excluded by the shopper: {exclude}"
    return text + "."


# ---------- model ----------

def cache_key(model: str, c: dict) -> str:
    raw = json.dumps([model, PROMPT_VERSION, c["item"], c["exclude"], c["rule"], c["name"]], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_cache() -> dict:
    if not CACHE.exists():
        return {}
    with open(CACHE, encoding="utf-8") as f:
        return {(e := json.loads(line))["key"]: e for line in f if line.strip()}


def ask(client, model_id: str, batch: list[dict]) -> tuple[dict[int, dict], dict]:
    first = batch[0]
    lines = "\n".join(f"{i}. {c['name']}" for i, c in enumerate(batch, 1))
    prompt = f"{describe(first['item'], first['exclude'], first['rule'])}\n\nProducts:\n{lines}"
    resp = client.messages.create(
        model=model_id, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    answers = {j["id"]: j for j in json.loads(text)["judgements"]}
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens, "stop": resp.stop_reason}
    return answers, usage


def judge(todo: list[dict], model: str, cache: dict) -> None:
    """Ask the model about every case not yet in the cache (grouped per item, BATCH per request), and append
    the answers to the cache. Prints the measured cost from the API's token counts."""
    model_id, price_in, price_out = MODELS[model]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it as a Codespaces / Actions secret.")
    import anthropic
    client = anthropic.Anthropic()
    groups: dict[str, list[dict]] = {}
    for c in todo:
        groups.setdefault(c["item"], []).append(c)
    batches = [g[i:i + BATCH] for g in groups.values() for i in range(0, len(g), BATCH)]
    used_in = used_out = 0
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "a", encoding="utf-8") as f:
        for n, batch in enumerate(batches, 1):
            answers, usage = ask(client, model_id, batch)
            used_in += usage["input"]
            used_out += usage["output"]
            if usage["stop"] != "end_turn":
                print(f"  request {n}: stopped with {usage['stop']}; answers may be incomplete")
            for i, c in enumerate(batch, 1):
                a = answers.get(i)
                entry = {"key": cache_key(model_id, c), "model": model_id, "prompt": PROMPT_VERSION,
                         "item": c["item"], "name": c["name"],
                         "match": a["match"] if a else None, "reason": a["reason"] if a else "no answer"}
                cache[entry["key"]] = entry
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"  request {n}/{len(batches)}: {len(batch)} products, {usage['input']} in / {usage['output']} out tokens")
            time.sleep(0.5)
    print(f"measured cost: {used_in:,} input + {used_out:,} output tokens "
          f"= ${used_in / 1e6 * price_in + used_out / 1e6 * price_out:.4f}")


def lookup(cache: dict, item: str, rule: str, name: str, model: str = "haiku") -> dict | None:
    """The cached answer for one product, asked exactly as in the evaluation (item + rule, no exclusions)."""
    return cache.get(cache_key(MODELS[model][0], {"item": item, "exclude": "", "rule": rule, "name": name}))


def cmd_lidl(model: str) -> None:
    """Judge this week's Lidl candidates: the offers valid today and the remembered regular prices that the word
    rules and my exclusions let through for each list item. These are never reviewed by me, so this is where the
    model is used. recommend.py then drops what it rejects and lists it in the report."""
    from datetime import date
    from recommend import latest, lidl_history_regular, lidl_offer_rows   # here, to avoid a circular import
    today = date.today().isoformat()
    _, lidl = latest("lidl")
    offers = lidl_offer_rows(lidl, today, use_plus=True)
    history, _ = lidl_history_regular(today)
    cache = load_cache()
    found, todo = [], []
    for it in load_list():
        item, exclude, rule = it["item"].strip(), (it.get("exclude") or "").strip(), (it.get("rule") or "").strip()
        names = sorted({r["name"] for r in candidates(offers, item, exclude) + candidates(history, item, exclude)})
        for name in names:
            c = {"item": item, "exclude": "", "rule": rule, "name": name}
            found.append(c)
            if cache_key(MODELS[model][0], c) not in cache:
                todo.append(c)
    print(f"Lidl: {len(found)} candidates for {len({c['item'] for c in found})} items; {len(todo)} not judged yet")
    if todo:
        judge(todo, model, cache)
    for c in found:
        a = lookup(cache, c["item"], c["rule"], c["name"], model)
        verdict = "no answer" if a is None or a["match"] is None else ("match" if a["match"] else "REJECTED")
        print(f"  {verdict:9} {c['item'][:28]:28} {c['name'][:50]:50} | {(a or {}).get('reason', '')}")


# ---------- eval ----------

def cmd_eval(store: str, model: str, dry_run: bool) -> None:
    model_id, price_in, price_out = MODELS[model]
    all_cases = cases(store)
    if not all_cases:
        sys.exit(f"No cases: are there reviewed products in data/shopping/{store}_products.csv for the list items?")
    cache = load_cache()
    todo = [c for c in all_cases if cache_key(model_id, c) not in cache]
    groups: dict[str, list[dict]] = {}
    for c in todo:
        groups.setdefault(c["item"], []).append(c)
    batches = [g[i:i + BATCH] for g in groups.values() for i in range(0, len(g), BATCH)]

    rejected = sum(not c["label"] for c in all_cases)
    print(f"{store}: {len(all_cases)} cases ({len(all_cases) - rejected} I accepted, {rejected} I rejected) "
          f"for {len({c['item'] for c in all_cases})} items; {len(todo)} not in the cache -> {len(batches)} requests")
    if dry_run:
        # rough: ~4 characters per token; system prompt + item text per request, ~20 output tokens per product
        est_in = len(batches) * (len(SYSTEM) + 200) / 4 + sum(len(c["name"]) + 6 for c in todo) / 4
        est_out = len(todo) * 25
        print(f"estimate with {model} ({model_id}): ~{est_in:,.0f} input + ~{est_out:,.0f} output tokens "
              f"= ~${est_in / 1e6 * price_in + est_out / 1e6 * price_out:.3f}")
        return

    if todo:
        judge(todo, model, cache)

    # score
    rows, tally = [], {"agree": 0, "false_reject": 0, "false_accept": 0, "no_answer": 0}
    for c in all_cases:
        a = cache[cache_key(model_id, c)]
        if a["match"] is None:
            verdict = "no_answer"
        elif a["match"] == c["label"]:
            verdict = "agree"
        else:
            verdict = "false_reject" if c["label"] else "false_accept"
        tally[verdict] += 1
        rows.append({"item": c["item"], "product": c["name"], "my_decision": "match" if c["label"] else "no match",
                     "label_source": c["source"],
                     "model": "" if a["match"] is None else ("match" if a["match"] else "no match"),
                     "verdict": verdict, "reason": a["reason"]})
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"eval_{store}_{model}_{PROMPT_VERSION}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["verdict"] == "agree", r["item"], r["product"])))

    n = len(all_cases)
    caught = rejected - tally["false_accept"] - sum(1 for r in rows if r["verdict"] == "no_answer" and r["my_decision"] == "no match")
    print(f"\n== {store}, {model} ({model_id}, prompt {PROMPT_VERSION}): {n} cases")
    excl = sum(1 for c in all_cases if c["source"] == "exclusion")
    print(f"   labels: {n - rejected} match; {rejected} no match ({rejected - excl} rejected in review, "
          f"{excl} removed by my exclusions)")
    print(f"   word rules alone:  {n - rejected} of {n} right ({(n - rejected) / n:.0%}): they accept all {rejected} I rejected")
    print(f"   rules + my exclusions: {n - rejected + excl} of {n} right ({(n - rejected + excl) / n:.0%})")
    print(f"   with the model:    {tally['agree']} of {n} right ({tally['agree'] / n:.0%})")
    print(f"   wrong products caught: {caught} of {rejected}"
          + (f" (recall {caught / rejected:.0%})" if rejected else ""))
    for src in ("review", "exclusion"):
        neg = [r for r in rows if r["my_decision"] == "no match" and r["label_source"] == src]
        if neg:
            print(f"     from {src:9}: {sum(r['verdict'] == 'agree' for r in neg)} of {len(neg)} caught")
    print(f"   right products wrongly rejected: {tally['false_reject']} of {n - rejected}"
          f"   (these could hide a cheaper real match)")
    if tally["no_answer"]:
        print(f"   no answer: {tally['no_answer']}")
    per_item: dict[str, list[int]] = {}
    for r in rows:
        s = per_item.setdefault(r["item"], [0, 0])
        s[0] += r["verdict"] == "agree"
        s[1] += 1
    for item, (ok, total) in per_item.items():
        print(f"     {item:32} {ok}/{total}")
    print(f"   disagreements first in {out_path}")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["lidl"]:
        cmd_lidl("haiku")
    elif len(args) >= 2 and args[0] == "eval" and args[1] in ("continente", "pingodoce"):
        model = args[args.index("--model") + 1] if "--model" in args else "haiku"
        if model not in MODELS:
            sys.exit(f"--model must be one of {', '.join(MODELS)}")
        cmd_eval(args[1], model, "--dry-run" in args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
