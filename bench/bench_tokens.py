#!/usr/bin/env python3
"""
bench_tokens.py — token cost of pasting a CSV into an LLM vs querying it via csvql's MCP.

Answers 5 realistic questions about the data both ways and counts the tokens the model
would consume: the whole file (paste) vs the SQL + result rows (query). The query cost is
flat — independent of file size — while the paste cost grows linearly and overflows the
context window almost immediately.

Usage:
    ./bench/bench_tokens.py [CSV]     # default: bench/.taxi-data/sample.csv
    # get the sample first with:  ./bench/bench_taxi.sh --sample

Tokenizer: uses `tiktoken` (cl100k) if installed for exact counts; otherwise falls back to
a char-based estimate and says so. `pip install tiktoken` for the accurate numbers.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSVQL = os.path.join(ROOT, "zig-out", "bin", "csvql")
if not os.path.exists(CSVQL):
    CSVQL = subprocess.run(["bash", "-lc", "command -v csvql"], capture_output=True, text=True).stdout.strip() or CSVQL

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, ".taxi-data", "sample.csv")
CONTEXT_WINDOW = 200_000  # typical LLM context budget, for the "does it even fit" column

# --- tokenizer (exact if tiktoken is present, else honest approximation) --------------
try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")
    tok = lambda s: len(_enc.encode(s))
    TOKENIZER = "tiktoken cl100k (exact)"
except Exception:
    # CSV is token-dense; ~0.29 tok/char measured on this dataset vs tiktoken. Approximate.
    tok = lambda s: max(1, round(len(s) * 0.29))
    TOKENIZER = "char-estimate (APPROXIMATE — `pip install tiktoken` for exact)"

# 5 questions a user might ask an agent about this file, and the SQL that answers each.
QA = [
    ("How many trips per cab type?",
     "SELECT cab_type, COUNT(*) FROM '{f}' GROUP BY cab_type"),
    ("Average total fare by passenger count?",
     "SELECT passenger_count, AVG(total_amount) FROM '{f}' GROUP BY passenger_count"),
    ("Which year had the most trips?",
     "SELECT DATE_PART('year',pickup_datetime) AS y, COUNT(*) AS c FROM '{f}' GROUP BY y ORDER BY c DESC LIMIT 1"),
    ("Total number of trips?",
     "SELECT COUNT(*) FROM '{f}'"),
    ("Top 3 passenger counts by average tip?",
     "SELECT passenger_count, AVG(tip_amount) AS a FROM '{f}' GROUP BY passenger_count ORDER BY a DESC LIMIT 3"),
]

# One-time MCP tool-schema overhead the model pays once per conversation.
# These are the real schemas from src/mcp.zig, not an approximation of them.
# An earlier version of this file invented a csv_query taking {file, sql},
# which charged every call for a path the model already writes inside the SQL
# and so overstated csvql's own cost by about 9 tokens per query.
TOOL_SCHEMA = """{"name":"csv_query","description":"Execute a SQL query against CSV files and return results as JSON. Supports SELECT, WHERE, GROUP BY, ORDER BY, LIMIT, JOIN, COUNT/SUM/AVG/MIN/MAX, DISTINCT, LIKE. File paths must be single-quoted in FROM. Results are capped (~100 rows / 12KB): do NOT SELECT * on large files — aggregate (COUNT/SUM/AVG with GROUP BY) or add WHERE/LIMIT to answer questions without pulling raw rows. Example: SELECT dept, COUNT(*) FROM 'data.csv' GROUP BY dept","inputSchema":{"type":"object","properties":{"sql":{"type":"string","description":"SQL query with single-quoted file paths in FROM clause"}},"required":["sql"]}}
{"name":"csv_schema","description":"Show column names and a few sample rows from a CSV file. Call this before csv_query to understand column names and data types.","inputSchema":{"type":"object","properties":{"file":{"type":"string","description":"Path to the CSV file"}},"required":["file"]}}"""


def paste_tokens(path):
    with open(path, "r", errors="replace") as fh:
        return tok(fh.read())


def query_tokens(path):
    total = 0
    for question, sql in QA:
        q = sql.format(f=path)
        out = subprocess.run([CSVQL, q], capture_output=True, text=True).stdout
        total += tok(question) + tok(q) + tok(out)  # sent (question+SQL) + received (rows)
    return total


def duckdb_shell_tokens(path):
    """What the same four questions cost an agent that shells out to duckdb.

    Pasting the file is the wrong baseline to stop at: nobody attempts it for a
    large CSV, so beating it proves little. An agent with a shell tool can run
    `duckdb -c "SELECT ..."` today, and that is the comparison that decides
    whether csvql saves anything. Counted both ways because DuckDB's default
    output is a box-drawing table, which costs roughly 3x the tokens of -csv,
    and an agent has to know to pass the flag.
    """
    out = {}
    for flag, label in ((None, "box"), ("-csv", "csv")):
        total = 0
        for question, sql in QA:
            q = sql.format(f=path).replace(f"'{path}'", f"read_csv('{path}')")
            cmd = ["duckdb"] + ([flag] if flag else []) + ["-c", q]
            shown = "duckdb " + (flag + " " if flag else "") + '-c "' + q + '"'
            try:
                res = subprocess.run(cmd, capture_output=True, text=True).stdout
            except FileNotFoundError:
                return None
            total += tok(question) + tok(shown) + tok(res)
        out[label] = total
    return out


def main():
    if not os.path.exists(SRC):
        sys.exit(f"CSV not found: {SRC}\nGet the sample first:  ./bench/bench_taxi.sh --sample")
    if not os.path.exists(CSVQL):
        sys.exit("csvql not found (build: zig build -Doptimize=ReleaseFast)")

    lines = open(SRC, errors="replace").read().splitlines()
    header, body = lines[0], lines[1:]
    # ~417 bytes/row on this dataset → row counts approximating each size.
    sizes = [("1 MB", 2_400), ("10 MB", 24_000), ("100 MB", 240_000), ("full", len(body))]

    print(f"\n  Token cost: paste CSV into context  vs  query via csvql --mcp")
    print(f"  source={SRC}  rows={len(body):,}  tokenizer={TOKENIZER}\n")
    print(f"  {'size':>7} {'rows':>10} {'paste':>13} {'query':>9} {'ratio':>9}  fits {CONTEXT_WINDOW//1000}K?")

    schema_t = tok(TOOL_SCHEMA)
    for label, n in sizes:
        n = min(n, len(body))
        slice_path = os.path.join(HERE, ".taxi-data", f"slice_{label.replace(' ', '')}.csv")
        with open(slice_path, "w") as fh:
            fh.write(header + "\n" + "\n".join(body[:n]) + "\n")
        p = paste_tokens(slice_path)
        q = query_tokens(slice_path) + schema_t  # amortized one-time schema counted once
        os.remove(slice_path)
        fits = "yes" if p <= CONTEXT_WINDOW else "NO"
        print(f"  {label:>7} {n:>10,} {p:>13,} {q:>9,} {round(p / q):>8,}x  {fits}")

    print(f"\n  Query cost is flat — SQL + a few result rows, independent of file size.")
    print(f"  MCP tool schema: {schema_t} tokens, paid once per conversation (not per query).")

    # The honest competitor: an agent with a shell, not an agent pasting a file.
    dd = duckdb_shell_tokens(SRC)
    if dd is None:
        print("\n  duckdb not on PATH, skipping the shell comparison.\n")
    else:
        mcp = query_tokens(SRC)
        print(f"\n  Same {len(QA)} questions, same file, no paste anywhere:")
        print(f"    csvql via MCP                    {mcp:>6,} tokens")
        print(f"    duckdb via shell, -csv           {dd['csv']:>6,} tokens  ({dd['csv']/mcp:.2f}x)")
        print(f"    duckdb via shell, default output {dd['box']:>6,} tokens  ({dd['box']/mcp:.2f}x)")
        print("    The -csv row is the one to beat; the gap is small and the flag is")
        print("    easy for a model to forget, which is most of the difference.\n")


if __name__ == "__main__":
    main()
