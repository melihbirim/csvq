"""A tiny English-to-SQL grammar for csvql, powered by TypeSafe's Jev.

The point of this module is token economy. Querying a large CSV with a language
model normally means sending rows, so the bill grows with the file. Here the
model sees the header and three sample rows and nothing else, so **the token
cost of a question is the same for a 1 KB file and a 10 GB file** — it is a
function of how many columns you have, not how many rows.

It works because the model never writes SQL. This module owns a grammar:

    SELECT [DISTINCT] <cols | agg(col) | scalar(col)>
    FROM <file>
    [WHERE <col> <op> <value> [AND <col> <op> <value>]]
    [GROUP BY <col>] [HAVING COUNT(*) <op> <n>]
    [ORDER BY <col> <dir>] [LIMIT <n>]

and the model only picks which slots to fill, from candidates this code
produced first: the real header, a fixed operator set, the aggregate and scalar
names, and — for a string comparison — the column's actual values, which csvql
itself reads. Nothing the model returns is ever concatenated into SQL as text,
so there is no injection surface and nothing to validate afterwards.

Requires TYPESAFE_API_KEY. No dependencies beyond the standard library.
"""

import json
import os
import re
import urllib.error
import urllib.request

import csvql

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
NONE = "__none__"

# ── the grammar's fixed vocabulary ───────────────────────────────────────────

SHAPES = {
    "rows": "the matching rows themselves, or particular columns of them",
    "distinct_values": "the set of values in a column, deduplicated, with no counting",
    "one_number": "a single figure over all matching rows: a count, total, average or maximum",
    "per_category": "one row per distinct value of some column, each with its own count or total",
}

OPS = {
    "equal": "equals, is, named, exactly",
    "not_equal": "is not, excluding, other than",
    "greater_than": "more than, over, above, older than, after",
    "less_than": "fewer than, under, below, younger than, before",
    "greater_or_equal": "at least, from, no less than",
    "less_or_equal": "at most, up to, no more than",
    "contains": "contains, mentions, includes, has the word",
    "between": "between two values, in the range, from X to Y",
    "is_null": "is missing, is empty, has no value",
    "is_not_null": "is present, is filled in, has a value",
}
OP_SQL = {
    "equal": "=", "not_equal": "!=", "greater_than": ">",
    "less_than": "<", "greater_or_equal": ">=", "less_or_equal": "<=",
}

AGGS = {
    "count": "how many rows",
    "count_distinct": "how many different values a column has",
    "sum": "the total of a numeric column",
    "avg": "the average, mean or typical value",
    "min": "the smallest, earliest or cheapest value",
    "max": "the largest, latest or most expensive value",
    "median": "the middle value, robust to outliers",
    "stddev_samp": "the spread or variability",
    "group_concat": "every value joined into one list",
}
AGG_SQL = {
    "count": "COUNT(*)", "count_distinct": "COUNT(DISTINCT {c})", "sum": "SUM({c})",
    "avg": "AVG({c})", "min": "MIN({c})", "max": "MAX({c})", "median": "MEDIAN({c})",
    "stddev_samp": "STDDEV({c})", "group_concat": "GROUP_CONCAT({c})",
}

SCALARS = {
    "upper": "in capitals", "lower": "in lower case", "trim": "with whitespace removed",
    "length": "how long the text is", "abs": "ignoring any minus sign",
    "round_op": "rounded to a whole number",
    "year": "just the year part of a date", "month": "the year and month of a date",
}
SCALAR_SQL = {
    "upper": "UPPER({c})", "lower": "LOWER({c})", "trim": "TRIM({c})",
    "length": "LENGTH({c})", "abs": "ABS({c})", "round_op": "ROUND({c}, 0)",
    "year": "STRFTIME('%Y', {c})", "month": "STRFTIME('%Y-%m', {c})",
}


class JevError(RuntimeError):
    pass


def _call(state, questions, api_key):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"state": state, "model": MODEL, "questions": questions}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise JevError(f"{e.code}: {e.read()[:300].decode(errors='replace')}") from None


def peek(csv_path, n=3):
    """Header plus a few rows — the entire payload the model ever sees.

    Three rows, not thirty: the model needs to know that `created_at` holds
    dates and `body` holds sentences, and a third row adds tokens without
    adding that. This is the whole reason the cost does not scale with the file.
    """
    rows = csvql.query(f"SELECT * FROM '{csv_path}' LIMIT {n}")
    if not rows:
        raise RuntimeError(f"{csv_path} has no rows")
    return list(rows[0].keys()), rows


def _col_choice(columns, rows):
    c = {col: f"holds values like {str(rows[0].get(col, ''))[:50]!r}" for col in columns}
    c[NONE] = "the question does not name a column here"
    return c


def plan(question, columns, rows, api_key=None):
    """One request. Every slot question is independent, so they run in parallel
    and the schema is sent once; most are speculative and simply go unread."""
    api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise JevError("TYPESAFE_API_KEY is not set")

    any_col = _col_choice(columns, rows)
    q = {
        "shape": {"type": "choice",
                  "instructions": "What shape of answer does `question` want? Pick the single best fit.",
                  "criteria": SHAPES},
        "aggregate": {"type": "choice",
                      "instructions": ("What single value does `question` ask to be computed ACROSS "
                                       "rows? Choose none if it asks about each row separately."),
                      "criteria": {**AGGS, NONE: "no aggregate; it wants the rows"}},
        "agg_column": {"type": "choice",
                       "instructions": "Which column is that computed over?", "criteria": any_col},
        "projection": {"type": "choice",
                       "instructions": ("Which single column does `question` most want to see? None "
                                        "if it wants whole rows."),
                       "criteria": any_col},
        "scalar": {"type": "choice",
                   "instructions": "How should that column be transformed for display, if at all?",
                   "criteria": {**SCALARS, NONE: "untransformed"}},
        "wants_filter": {"type": "noul",
                         "instructions": "Does `question` restrict which rows to consider?",
                         "criteria": {"true": "Names a condition rows must meet.",
                                      "false": "Asks about all rows."}},
        "filter_column": {"type": "choice",
                          "instructions": "Which column does the restriction apply to?",
                          "criteria": any_col},
        "filter_operator": {"type": "choice",
                            "instructions": "How does `question` compare that column?",
                            "criteria": OPS},
        "wants_second_filter": {"type": "noul",
                                "instructions": ("Does `question` apply a SECOND condition on a "
                                                 "DIFFERENT column?"),
                                "criteria": {"true": "Two conditions joined by and.",
                                             "false": "One condition or none."}},
        "filter2_column": {"type": "choice",
                           "instructions": "Which column does the second condition apply to?",
                           "criteria": any_col},
        "filter2_operator": {"type": "choice",
                             "instructions": "How does it compare that second column?",
                             "criteria": OPS},
        # Wording matters more than it looks: an earlier version asked "which
        # column is the category?" and a file with a column literally named
        # `category` got that column every time, whatever the question said.
        # Instructions must not reuse words likely to appear as column names.
        "group_column": {"type": "choice",
                         "instructions": ("`question` asks for one row per group. Which column's "
                                          "values name those groups? Read the question, not the "
                                          "column names, to decide."),
                         "criteria": any_col},
        "wants_having": {"type": "noul",
                         "instructions": ("Does `question` keep only the categories whose COUNT meets "
                                          "a threshold, such as 'cities with more than 3 people'?"),
                         "criteria": {"true": "The threshold is on a per-category count.",
                                      "false": "No condition on the categories."}},
        "wants_order": {"type": "noul",
                        "instructions": "Does `question` ask for a ranking or a top/bottom slice?",
                        "criteria": {"true": "Says top, highest, lowest, most, least, sorted.",
                                     "false": "No ordering requested."}},
        "order_direction": {"type": "choice",
                            "instructions": "Which end first?",
                            "criteria": {"desc": "largest, highest, most, top, latest",
                                         "asc": "smallest, lowest, fewest, earliest"}},
        "wants_limit": {"type": "noul",
                        "instructions": "Does `question` ask for only a fixed number of rows?",
                        "criteria": {"true": "Says top N, first N, a few.",
                                     "false": "Wants every matching row."}},
        "needs_judgment": {"type": "noul",
                           "instructions": ("Does `question` need free text to be READ and judged — "
                                            "tone, intent, sentiment — rather than compared exactly?"),
                           "criteria": {"true": "Asks about anger, confusion, risk of leaving.",
                                        "false": "Every condition is an exact comparison."}},
    }

    resp = _call({"question": question, "columns": columns, "sample_rows": rows}, q, api_key)
    a = resp["answers"]
    ch = lambda k: (None if a[k]["choice"] == NONE else a[k]["choice"])  # noqa: E731
    yes = lambda k: a[k]["noul"] >= 0.5  # noqa: E731

    shape = a["shape"]["choice"]
    agg = ch("aggregate")
    # The shape decides which slots are real. Reading them all independently is
    # what produces nonsense like SELECT city, COUNT(DISTINCT city) GROUP BY city.
    if shape in ("rows", "distinct_values"):
        agg = None
    elif not agg:
        agg = "count"

    return {
        "shape": shape,
        "aggregate": agg,
        "agg_column": ch("agg_column"),
        "projection": ch("projection"),
        "scalar": ch("scalar"),
        "filters": [f for f in (
            {"column": ch("filter_column"), "operator": a["filter_operator"]["choice"]}
            if yes("wants_filter") and ch("filter_column") else None,
            {"column": ch("filter2_column"), "operator": a["filter2_operator"]["choice"]}
            if yes("wants_second_filter") and ch("filter2_column") else None,
        ) if f],
        "group_column": ch("group_column") if shape == "per_category" else None,
        "having": yes("wants_having"),
        "order_direction": a["order_direction"]["choice"] if yes("wants_order") else None,
        "limit": yes("wants_limit"),
        "needs_judgment": a["needs_judgment"]["noul"],
        "numbers": re.findall(r"-?\d+(?:\.\d+)?", question),
        "input_tokens": resp.get("usage", {}).get("input_tokens", 0),
    }


def resolve_literal(question, csv_path, column, api_key=None, cap=40):
    """Pick which of the column's real values the question meant.

    Selection, not generation: the literal that reaches the SQL is a value
    csvql read out of the file a moment ago, never a string the model wrote.
    """
    api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
    rows = csvql.query(f"SELECT DISTINCT {column} FROM '{csv_path}' LIMIT {cap + 1}")
    values = [str(r[column]) for r in rows if str(r[column]).strip()]
    if not values or len(values) > cap:
        return None, 0
    resp = _call(
        {"question": question, "column": column, "possible_values": values},
        {"value": {"type": "choice",
                   "instructions": (f"`question` filters `{column}`. Which of its actual values "
                                    f"does the question mean?"),
                   "criteria": {v: f"the value {v!r}" for v in values}}},
        api_key,
    )
    return resp["answers"]["value"]["choice"], resp.get("usage", {}).get("input_tokens", 0)


def to_sql(csv_path, p, literals=None):
    """Assemble the query. Every fragment is a fixed keyword or a value that came
    from the header or from the file itself."""
    literals, nums = literals or {}, list(p["numbers"])

    if p["aggregate"] == "count":
        select = "COUNT(*)"
    elif p["aggregate"] and p["agg_column"]:
        select = AGG_SQL[p["aggregate"]].format(c=p["agg_column"])
    elif p["projection"]:
        select = (SCALAR_SQL[p["scalar"]].format(c=p["projection"])
                  if p["scalar"] else p["projection"])
    else:
        select = "*"

    group = p["group_column"]
    if group and not select.startswith(group):
        select = f"{group}, {select}" if select != "*" else f"{group}, COUNT(*)"

    head = "SELECT DISTINCT " if p["shape"] == "distinct_values" else "SELECT "
    sql = f"{head}{select} FROM '{csv_path}'"

    preds = []
    for i, f in enumerate(p["filters"]):
        col, op = f["column"], f["operator"]
        if op == "is_null":
            preds.append(f"{col} IS NULL")
        elif op == "is_not_null":
            preds.append(f"{col} IS NOT NULL")
        elif op == "between" and len(nums) >= 2:
            preds.append(f"{col} BETWEEN {nums[0]} AND {nums[1]}"); nums = nums[2:]
        elif op == "contains" and literals.get(i):
            preds.append(f"{col} LIKE '%{literals[i]}%'")
        elif literals.get(i) is not None:
            v = str(literals[i]).replace("'", "''")
            preds.append(f"{col} {OP_SQL.get(op, '=')} '{v}'")
        elif nums:
            preds.append(f"{col} {OP_SQL.get(op, '=')} {nums[0]}"); nums = nums[1:]
    if preds:
        sql += " WHERE " + " AND ".join(preds)

    if group:
        sql += f" GROUP BY {group}"
        if p["having"] and nums:
            sql += f" HAVING COUNT(*) > {nums[0]}"; nums = nums[1:]

    if p["order_direction"]:
        sql += f" ORDER BY {select.split(',')[-1].strip()} {p['order_direction'].upper()}"
    if p["limit"] and nums:
        sql += f" LIMIT {nums[0]}"
    return sql


def ask(csv_path, question, api_key=None, run=True):
    """English in, rows out. Returns (sql, rows, input_tokens)."""
    columns, rows = peek(csv_path)
    p = plan(question, columns, rows, api_key)
    tokens = p["input_tokens"]

    literals = {}
    for i, f in enumerate(p["filters"]):
        if not f["column"] or f["operator"] in ("is_null", "is_not_null", "between"):
            continue
        sample = str(rows[0].get(f["column"], ""))
        if sample.replace(".", "", 1).lstrip("-").isdigit():
            continue  # numeric column: the value comes from the question
        lit, t = resolve_literal(question, csv_path, f["column"], api_key)
        tokens += t
        if lit is not None:
            literals[i] = lit

    sql = to_sql(csv_path, p, literals)
    return sql, (csvql.query(sql) if run else None), tokens
