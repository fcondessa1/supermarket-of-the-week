"""Everyone's shopping lists: storage, which products count for each item, and the weekly product lists.

Usage:
    python src/lists.py migrate <user>      # once: my old list and review decisions become <user>'s list and overrides
    python src/lists.py build               # weekly, before the price runs: products to price, from everyone's lists

Per person, in the repository (so the Tuesday run sees every list, and every change is versioned):
    data/users/<user>/list.csv        item, quantity, unit (l / kg / unit), rule, notes
    data/users/<user>/overrides.csv   store, item, product_id, name, counts (yes / no), date

Which products count for an item, for one person:
    1. their override, if they made one (one click in the app);
    2. otherwise the model's answer for the item and THEIR rule (src/llm_match.py, the prompt scored at 93% on a
       held-out store); the model never sees `notes`;
    3. otherwise not judged yet.
Candidates come from word matching (every word of the item in the product's name), as before; exclusions are
gone: say it in the rule instead ("not frozen; not freeze-dried").

`build` writes what the existing price fetchers read, so they run unchanged:
    data/shopping/<store>_products.csv   every product that counts for someone's item (unavailable marks kept)
    data/shopping/list.csv               every item on anyone's list
Storage: files are read from disk. Writes go to disk and, when GITHUB_TOKEN and GITHUB_REPO are set (the
deployed app), also to the repository through GitHub's contents API: one commit per save.
"""

import base64
import csv
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from catalogue import STORES, candidates, catalogue_path, normalise, tokens
from llm_match import CACHE, MODELS, cache_key, judge, load_cache, lookup

USERS = Path("data/users")
LIST_FIELDS = ["item", "quantity", "unit", "rule", "notes"]
OVERRIDE_FIELDS = ["store", "item", "product_id", "name", "counts", "date"]
SHOPS = {"Continente": "continente", "Pingo Doce": "pingodoce"}
MAX_CANDIDATES = 400          # per item and store: above this, ask for more words instead of judging them all
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


# ---------- storage ----------

def _github(path: str, text: str | None = None, append: bool = False, message: str = "") -> None:
    """Write one file to the repository (the deployed app's disk is a copy that is replaced on redeploy).
    append: add text to the end of the repository's current version (for the shared model cache)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return
    import requests
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    for _ in range(3):                                   # retry if someone else committed in between
        r = requests.get(url, headers=headers, timeout=30)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = text
        if append and r.status_code == 200:
            body = base64.b64decode(r.json()["content"]).decode("utf-8") + text
        data = {"message": message or f"app: update {path}",
                "content": base64.b64encode(body.encode("utf-8")).decode()}
        if sha:
            data["sha"] = sha
        put = requests.put(url, headers=headers, json=data, timeout=30)
        if put.status_code in (200, 201):
            return
        if put.status_code != 409:
            raise RuntimeError(f"GitHub refused to save {path}: {put.status_code} {put.text[:200]}")
    raise RuntimeError(f"Could not save {path}: it kept changing on GitHub; try again")


def write_csv(path: Path, fields: list[str], rows: list[dict], message: str = "") -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")
    _github(path.as_posix(), buf.getvalue(), message=message)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def users() -> list[str]:
    return sorted(p.name for p in USERS.iterdir() if p.is_dir()) if USERS.exists() else []


def load_list(user: str) -> list[dict]:
    return [{k: (r.get(k) or "").strip() for k in LIST_FIELDS} for r in read_csv(USERS / user / "list.csv")
            if (r.get("item") or "").strip()]


def save_list(user: str, rows: list[dict]) -> None:
    rows = [{k: str(r.get(k) or "").strip() for k in LIST_FIELDS} for r in rows if str(r.get("item") or "").strip()]
    write_csv(USERS / user / "list.csv", LIST_FIELDS, rows, f"app: {user} updated their list")


def load_overrides(user: str) -> dict[tuple[str, str, str], dict]:
    """(store, item, product key) -> override row; the product key is the product id, or the name if none."""
    return {(r["store"], r["item"], r.get("product_id") or r["name"]): r
            for r in read_csv(USERS / user / "overrides.csv")}


def save_overrides(user: str, overrides: dict) -> None:
    rows = sorted(overrides.values(), key=lambda r: (r["item"], r["store"], r["name"]))
    write_csv(USERS / user / "overrides.csv", OVERRIDE_FIELDS, rows, f"app: {user} corrected product matches")


def push_cache(entries: list[dict]) -> None:
    """New model answers are already in the local cache file; send them to the repository's copy too."""
    if entries:
        _github(CACHE.as_posix(), "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                append=True, message=f"app: {len(entries)} new model answers")


# ---------- which products count ----------

def verdict_for(user: str, cache: dict | None = None):
    """The verdict function recommend.compute() takes, for one person."""
    overrides = load_overrides(user)
    by_name = {(o["store"], o["item"], normalise(o["name"])): o for o in overrides.values()}
    cache = load_cache() if cache is None else cache

    def verdict(store: str, item_row: dict, name: str, product_id: str) -> tuple[bool | None, str]:
        item = item_row["item"].strip()
        # by id, else by name: Lidl's remembered prices carry no id, and catalogue names are lowercase
        o = overrides.get((store, item, product_id)) if product_id else None
        o = o or by_name.get((store, item, normalise(name)))
        if o:
            return o["counts"] == "yes", "your choice"
        a = lookup(cache, item, (item_row.get("rule") or "").strip(), name)
        if a and a.get("match") is not None:
            return bool(a["match"]), a.get("reason", "")
        return None, "not judged yet"
    return verdict


def load_catalogue(store: str) -> list[dict]:
    rows = read_csv(catalogue_path(store))
    return [r | {"_tokens": tokens(r["name"]), "_norm": normalise(r["name"])} for r in rows]


def lidl_products() -> list[dict]:
    """This week's lidl.pt offers plus every product ever seen there (for remembered prices before offers)."""
    seen: dict[str, dict] = {}
    for path in sorted(Path("data/prices/lidl").glob("*.csv")):
        for r in read_csv(path):
            if r.get("name"):
                seen[r.get("product_id") or r["name"]] = {"product_id": r.get("product_id", ""), "name": r["name"]}
    return [r | {"_tokens": tokens(r["name"]), "_norm": normalise(r["name"])} for r in seen.values()]


def candidate_products(item: str, catalogues: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """store -> products whose name has all the item's words."""
    return {store: candidates(rows, item, "") for store, rows in catalogues.items()}


def unjudged(user_items: list[tuple[str, dict]], catalogues: dict[str, list[dict]], cache: dict,
             model: str = "haiku") -> list[dict]:
    """Model cases still to ask: one per (item, rule, product name), across all stores and the given items."""
    todo, keys = [], set()
    for _, it in user_items:
        item, rule = it["item"], (it.get("rule") or "").strip()
        for store, cands in candidate_products(item, catalogues).items():
            for r in cands[:MAX_CANDIDATES]:
                c = {"item": item, "exclude": "", "rule": rule, "name": r["name"]}
                k = cache_key(MODELS[model][0], c)
                if k not in cache and k not in keys:
                    keys.add(k)
                    todo.append(c)
    return todo


def all_catalogues() -> dict[str, list[dict]]:
    cats = {label: load_catalogue(folder) for label, folder in SHOPS.items()}
    cats["Lidl"] = lidl_products()
    return cats


# ---------- commands ----------

def cmd_build() -> None:
    """Products to price this week: for every person's items, what counts for them (override, else model)."""
    people = users()
    if not people:
        sys.exit(f"No lists in {USERS}. Run: python src/lists.py migrate <your name>")
    items = [(u, it) for u in people for it in load_list(u)]
    catalogues = all_catalogues()
    missing = [s for s, rows in catalogues.items() if s != "Lidl" and not rows]
    if missing:
        sys.exit(f"No catalogue for {', '.join(missing)}. Run: python src/catalogue.py <store> build")
    cache = load_cache()
    todo = unjudged(items, catalogues, cache)
    print(f"{len(people)} people, {len(items)} list items; {len(todo)} product judgements still to ask the model")
    if todo:
        judge(todo, "haiku", cache)

    for label, folder in SHOPS.items():
        old = {(r["item"], r["product_id"]): r["decision"] for r in read_csv(Path(f"data/shopping/{folder}_products.csv"))}
        out: dict[tuple[str, str], dict] = {}
        for user, it in items:
            verdict = verdict_for(user, cache)
            for r in candidate_products(it["item"], {label: catalogues[label]})[label][:MAX_CANDIDATES]:
                ok, _ = verdict(label, it, r["name"], r["product_id"])
                if ok:
                    key = (it["item"], r["product_id"])
                    decision = "unavailable" if old.get(key) == "unavailable" else "accepted"
                    out[key] = {"item": it["item"], "product_id": r["product_id"], "name": r["name"],
                                "url": r.get("url", ""), "decision": decision}
        rows = sorted(out.values(), key=lambda r: (r["item"], r["name"]))
        write_csv(Path(f"data/shopping/{folder}_products.csv"), ["item", "product_id", "name", "url", "decision"], rows)
        print(f"{label}: {sum(r['decision'] == 'accepted' for r in rows)} products to price "
              f"({sum(r['decision'] == 'unavailable' for r in rows)} marked unavailable)")

    union: dict[str, dict] = {}
    for _, it in items:
        union.setdefault(it["item"], it)
    write_csv(Path("data/shopping/list.csv"), LIST_FIELDS, list(union.values()))
    print(f"{len(union)} different items -> data/shopping/list.csv")


def cmd_migrate(user: str) -> None:
    """My old list (exclusions folded into the rule) and my review decisions (as overrides) become <user>'s."""
    if not NAME_RE.match(user):
        sys.exit("Use a short lowercase name: letters, digits, - or _")
    if (USERS / user / "list.csv").exists():
        sys.exit(f"{USERS / user} already has a list; not overwriting it")
    rows = []
    for r in read_csv(Path("data/shopping/list.csv")):
        rule = (r.get("rule") or "").strip()
        excl = [x.strip() for x in (r.get("exclude") or "").split(";") if x.strip()]
        if excl:
            rule = "; ".join([rule] if rule else []) + ("; " if rule else "") + "not: " + ", ".join(excl)
        rows.append({"item": r["item"], "quantity": r.get("quantity", ""), "unit": r.get("unit", ""),
                     "rule": rule, "notes": r.get("notes", "")})
    save_list(user, rows)
    overrides = {}
    today = date.today().isoformat()
    for label, folder in SHOPS.items():
        for d in read_csv(Path(f"data/shopping/{folder}_products.csv")):
            counts = "no" if d["decision"] == "rejected" else "yes"
            overrides[(label, d["item"], d["product_id"])] = {"store": label, "item": d["item"],
                                                              "product_id": d["product_id"], "name": d["name"],
                                                              "counts": counts, "date": today}
    save_overrides(user, overrides)
    print(f"{user}: {len(rows)} items, {len(overrides)} of my review decisions kept as overrides -> {USERS / user}")


def main() -> None:
    args = sys.argv[1:]
    if args == ["build"]:
        cmd_build()
    elif len(args) == 2 and args[0] == "migrate":
        cmd_migrate(args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
