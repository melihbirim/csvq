#!/usr/bin/env python3
"""Ask one question per SQL feature and check the planned SQL, then run it.

Measures what the planner actually covers instead of asserting it. Each case is
(question, substrings the SQL must contain). Costs one Jev call per case, plus
one more where a string literal has to be resolved.

    TYPESAFE_API_KEY=... CSVQL=... python3 coverage_test.py
"""
import os, sys, traceback
from plan import JevError, build_sql, csvql, distinct_values, plan, resolve_literal, sample

CSV = sys.argv[1]
KEY = os.environ["TYPESAFE_API_KEY"]
BIN = os.environ.get("CSVQL", "csvql")

# Fragments that must NOT appear, per case.
FORBIDDEN = {
    "distinct":      ["COUNT(", "GROUP BY"],
    "having":        ["COUNT(DISTINCT"],
    "two_filters":   ["COUNT("],
    "scalar_length": ["AVG(", "COUNT("],
    "select_all":    ["WHERE", "GROUP BY"],
    "count":         ["GROUP BY", "WHERE"],
}

CASES = [
    ("select_all",        "show me everything",                             ["SELECT *"]),
    ("count",             "how many rows are there",                        ["COUNT(*)"]),
    ("where_numeric",     "people over 40",                                 ["WHERE", "age", ">", "40"]),
    ("where_string",      "everyone in Berlin",                             ["WHERE", "city"]),
    ("two_filters",       "people over 40 in Berlin",                       ["WHERE", "age", "AND", "city"]),
    ("group_by",          "how many people per city",                       ["GROUP BY", "city"]),
    ("avg",               "what is the average age",                        ["AVG(age)"]),
    ("sum_group",         "total age by city",                              ["SUM(age)", "GROUP BY"]),
    ("order_desc",        "cities with the most people, highest first",     ["ORDER BY", "DESC"]),
    ("limit",             "show me the first 5 people",                     ["LIMIT 5"]),
    ("distinct",          "which distinct cities appear",                   ["SELECT DISTINCT", "city"]),
    ("min_max",           "what is the oldest age",                         ["MAX(age)"]),
    ("median",            "what is the median age",                         ["MEDIAN(age)"]),
    ("stddev",            "how much does age vary",                         ["STDDEV"]),
    ("between",           "people aged between 30 and 50",                  ["BETWEEN", "30", "50"]),
    ("scalar_upper",      "show me the city names in capitals",             ["UPPER(city)"]),
    ("scalar_length",     "how long is each review",                        ["LENGTH(review)"]),
    ("having",            "cities with more than 3 people",                 ["GROUP BY", "HAVING", "COUNT(*)"]),
    ("count_distinct",    "how many different cities are there",            ["COUNT(DISTINCT"]),
    ("contains",          "reviews mentioning refund",                      ["LIKE"]),
]

cols, rows = sample(BIN, CSV)
ok = fail = 0
tokens = 0
print(f"{len(CASES)} cases against {CSV} ({len(cols)} columns)\n")
for name, q, expect in CASES:
    try:
        p = plan(q, cols, rows, KEY)
        tokens += p["usage"].get("input_tokens", 0)
        # Resolve a literal per filter. Gating this on "the question has no
        # numbers" silently dropped the string half of "over 40 in Berlin".
        lits = {}
        for i, f in enumerate(p["filters"]):
            if not f["column"] or f["operator"] in ("is_null", "is_not_null", "between"):
                continue
            sample_val = str(rows[0].get(f["column"], ""))
            is_numeric = sample_val.replace(".", "", 1).lstrip("-").isdigit()
            if is_numeric:
                continue
            cands = distinct_values(BIN, CSV, f["column"])
            if cands:
                r = resolve_literal(q, f["column"], cands, KEY)
                if r: lits[i] = r[0]
        sql = build_sql(CSV, p, lits)
        # Substring matching gave false passes: COUNT(DISTINCT city) satisfied a
        # check for "DISTINCT" while being the wrong query. Forbidden fragments
        # catch that.
        norm = sql.upper()
        missing = [e for e in expect if e.upper() not in norm]
        missing += [f"UNWANTED {b}" for b in FORBIDDEN.get(name, []) if b.upper() in norm]
        runs = True; err = ""
        try: csvql(BIN, sql)
        except Exception as e: runs = False; err = str(e)[:60]
        if missing or not runs:
            fail += 1
            print(f"  FAIL {name:16s} {q!r}")
            print(f"       {sql}")
            if missing: print(f"       missing: {missing}")
            if not runs: print(f"       did not run: {err}")
        else:
            ok += 1
            print(f"  ok   {name:16s} {sql[:96]}")
    except Exception as e:
        fail += 1
        print(f"  ERR  {name:16s} {e}")
print(f"\n{ok}/{len(CASES)} planned and ran correctly, {tokens:,} input tokens, ${tokens/1e6*0.042:.5f}")
