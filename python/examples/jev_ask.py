"""Query a large CSV in English, for a token cost that does not grow with the file.

Run from the repo root:
    export TYPESAFE_API_KEY=...
    python3 python/examples/jev_ask.py                      # demo on test.csv
    python3 python/examples/jev_ask.py big.csv "top 5 cities by revenue"
    python3 python/examples/jev_ask.py big.csv "..." --explain

The model is sent the header and three sample rows. It is never sent the file,
and it never writes SQL — it picks slots in the grammar in `jev_grammar.py`,
and csvql runs the result. So a question costs the same number of tokens
whether the CSV is 1 KB or 10 GB.

This is an example, not part of the csvql package. `import csvql` still gives
you exactly `query`, `query_csv`, `query_df` and `query_tuples`; nothing here
is installed by `pip install csvql-query`, and the engine itself makes no
network calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import csvql  # noqa: E402
from jev_grammar import JevError, ask, peek, plan, to_sql  # noqa: E402

PRICE_PER_MTOK = 0.042  # TypeSafe list price for Jev input tokens; output is free


def show(csv_path, question, explain=False):
    try:
        if explain:
            columns, rows = peek(csv_path)
            p = plan(question, columns, rows)
            sql, result, tokens = to_sql(csv_path, p), None, p["input_tokens"]
        else:
            sql, result, tokens = ask(csv_path, question)
    except JevError as e:
        sys.exit(f"jev: {e}")
    except Exception as e:
        sys.exit(f"failed: {e}")

    print(f"  ?  {question}")
    print(f"  >  {sql}")
    print(f"     {tokens:,} input tokens, ${tokens / 1e6 * PRICE_PER_MTOK:.6f}, 0 rows sent")
    if result is not None:
        for row in result[:5]:
            print(f"     {row}")
        if len(result) > 5:
            print(f"     ... {len(result) - 5} more rows")
    print()


def demo():
    """The point of the example: same question, files four orders of magnitude apart."""
    print(__doc__.split("Run from")[0].strip(), "\n")

    print("=== a question against the tiny sample file ===\n")
    for q in ("how many rows are there",
              "everyone over 26",
              "which distinct cities appear"):
        show("test.csv", q)

    big = os.environ.get("BIG_CSV")
    if not big or not os.path.exists(big):
        print("Set BIG_CSV=/path/to/a/large.csv to see the token cost hold flat\n"
              "as the file grows. That is the whole claim, and it is worth checking\n"
              "rather than believing.")
        return

    size_gb = os.path.getsize(big) / 1e9
    print(f"=== the same shape of question against {size_gb:.2f} GB ===\n")
    show(big, "how many rows are there")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    explain = "--explain" in sys.argv
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set")
    if len(args) >= 2:
        show(args[0], " ".join(args[1:]), explain)
    else:
        demo()


if __name__ == "__main__":
    main()
