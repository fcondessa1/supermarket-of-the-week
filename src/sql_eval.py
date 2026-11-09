"""Questions about my price history in plain words, answered by a model that writes SQL (option 3).

Usage:
    python src/sql_eval.py check          # run every reference answer: I verify them by hand before any model run

data/llm/sql_questions.csv: id, split (dev = for tuning the prompt, test = scored once at the end), question,
reference_sql (empty = the data can't answer it: the right response is to say so, not to guess).
The questions were written on 2026-11-09, before the history had enough weeks to answer most of them well,
so they couldn't be chosen to suit the data. Answers are computed fresh on every run, so they follow the data.

Coming once there are 4-6 weeks of data: `run --split dev --model haiku`. The model gets the table
descriptions (python src/history.py schema) and a question, returns SQL or "cannot answer"; its result is
compared with the reference result (same rows, numbers within 0.01).
"""

import csv
import sys
from pathlib import Path

from history import connect

QUESTIONS = Path("data/llm/sql_questions.csv")


def main() -> None:
    if sys.argv[1:] != ["check"]:
        sys.exit(__doc__)
    con = connect()
    with open(QUESTIONS, encoding="utf-8") as f:
        questions = list(csv.DictReader(f))
    for q in questions:
        print(f"\n{q['id']} [{q['split']}] {q['question']}")
        if not q["reference_sql"]:
            print("   -> not answerable from the data (the model should say so)")
            continue
        try:
            rows = con.sql(q["reference_sql"]).fetchall()
        except Exception as e:                          # a reference that fails is a bug in the question set
            print(f"   -> ERROR: {str(e).splitlines()[0]}")
            continue
        shown = "; ".join(", ".join("" if v is None else str(v) for v in r) for r in rows[:6])
        print(f"   -> {shown or '(no rows)'}" + (f"  (+{len(rows) - 6} more rows)" if len(rows) > 6 else ""))


if __name__ == "__main__":
    main()
