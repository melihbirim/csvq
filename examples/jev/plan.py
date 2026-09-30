"""Turn an English question into SQL that csvql can run.

The model never writes SQL. It fills slots in a grammar this file owns, whose
aggregate and scalar lists come from `capabilities.py`, which reads them out of
csvql's own Zig enums. Every slot is a Choice over candidates built first by
code — the file's real header, a fixed keyword set, or for a string comparison
the column's actual distinct values, which csvql itself supplies.

So the model selects; it cannot invent a column, an operator, or a literal that
is not in the data. There is no generated fragment to sanitize and no SQL to
validate after the fact.

One request per question. The slot questions are independent, so they run in
parallel and the schema is sent once; most are speculative, and a question
with no grouping still gets asked about grouping. Only the header and five
sample rows leave the machine. No row of the data itself is ever sent.
"""

import csv as _csv
import io
import json
import re
import subprocess
import urllib.error
import urllib.request

import capabilities as cap

NONE = "__none__"

COMPARISON_OPS = {
    "equal": "equals, is, named, exactly",
    "not_equal": "is not, excluding, other than",
    "greater_than": "more than, over, above, older than, after",
    "less_than": "fewer than, under, below, younger than, before",
    "greater_or_equal": "at least, from, no less than, minimum",
    "less_or_equal": "at most, up to, no more than, maximum",
    "contains": "contains, mentions, includes, has the word",
    "between": "between two values, in the range, from X to Y",
    "is_null": "is missing, is empty, has no value",
    "is_not_null": "is present, is filled in, has a value",
    "in_list": "is one of several named values",
}
OP_SQL = {
    "equal": "=", "not_equal": "!=", "greater_than": ">", "less_than": "<",
    "greater_or_equal": ">=", "less_or_equal": "<=",
}

# Built from the engine's own enums, so a new SQL function shows up here as
# soon as someone describes it in capabilities.py. test_drift.py fails until
# they do.
AGGREGATES = {a: cap.AGGREGATE_DESC[a] for a in cap.planner_aggregates()}
AGGREGATES[NONE] = "no aggregate; the question wants the rows themselves"
AGG_SQL = cap.AGGREGATE_SQL

SCALARS = {s: cap.SCALAR_DESC[s] for s in cap.planner_scalars()}
SCALARS.update({k: d for k, (d, _) in cap.DATE_PARTS.items()})
SCALARS[NONE] = "the column as it is, untransformed"
SCALAR_SQL = dict(cap.SCALAR_SQL)
SCALAR_SQL.update({k: t for k, (_, t) in cap.DATE_PARTS.items()})

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


class JevError(RuntimeError):
    pass


def call(state, questions, api_key, timeout=90):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"state": state, "model": MODEL, "questions": questions}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise JevError(f"{e.code}: {e.read()[:300].decode(errors='replace')}") from None


def csvql(bin_path, sql, timeout=120):
    out = subprocess.run([bin_path, sql], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    return out.stdout


def sample(bin_path, csv_path, n=5):
    """Header plus a few rows. Values matter as much as names: the model needs to
    see that `created_at` holds dates and `body` holds sentences."""
    rows = list(_csv.DictReader(io.StringIO(
        csvql(bin_path, f"SELECT * FROM '{csv_path}' LIMIT {n}")
    )))
    if not rows:
        raise RuntimeError("file has no rows")
    return list(rows[0].keys()), rows


def distinct_values(bin_path, csv_path, column, cap=40):
    """Candidate literals for a string comparison, straight from the file.

    This is what makes `WHERE country = 'JP'` safe to assemble: the literal is
    a value csvql read a moment ago, not a string the model produced.
    """
    try:
        out = csvql(bin_path, f"SELECT DISTINCT {column} FROM '{csv_path}' LIMIT {cap + 1}")
    except Exception:
        return []
    vals = [l.strip().strip('"') for l in out.splitlines()[1:] if l.strip()]
    return [] if len(vals) > cap else vals


def _col_criteria(columns, rows, extra=None):
    c = {col: f"holds values like {str(rows[0].get(col, ''))[:60]!r}" for col in columns}
    if extra:
        c.update(extra)
    return c


def plan(question, columns, rows, api_key):
    """One request, every slot in the grammar. Returns the filled plan."""
    numbers = re.findall(r"-?\d+(?:\.\d+)?", question)
    any_col = _col_criteria(columns, rows, {NONE: "the question does not name a column here"})

    q = {
        "shape": {
            "type": "choice",
            "instructions": (
                "What shape of answer does `question` want? These are alternatives; "
                "pick the single best fit."
            ),
            "criteria": {
                "rows": "the matching rows themselves, or particular columns of them",
                "distinct_values": "the set of values that appear in a column, deduplicated, "
                                   "with no counting",
                "one_number": "a single figure computed over all matching rows, such as a "
                              "count, total, average or maximum",
                "per_category": "one row per category, each with its own count or total",
            },
        },
        "aggregate": {
            "type": "choice",
            "instructions": (
                "What single value does `question` ask to be computed ACROSS rows? "
                "Choose none if it asks for something about each row separately, "
                "such as the length of each review or each name in capitals."
            ),
            "criteria": AGGREGATES,
        },
        "agg_column": {
            "type": "choice",
            "instructions": (
                "If `question` asks for a total, average, middle, spread, largest, "
                "smallest, or a count of distinct values, which column is that over?"
            ),
            "criteria": any_col,
        },
        "projection": {
            "type": "choice",
            "instructions": (
                "Which single column does `question` most want to see? Choose none "
                "if it wants whole rows or an aggregate rather than one column."
            ),
            "criteria": any_col,
        },
        "scalar": {
            "type": "choice",
            "instructions": "How should that column be transformed for display, if at all?",
            "criteria": SCALARS,
        },
        # -- filter ------------------------------------------------------------
        "wants_filter": {
            "type": "noul",
            "instructions": "Does `question` restrict which rows to consider?",
            "criteria": {
                "true": "Names a condition rows must meet, such as over 40, in Berlin, missing an email.",
                "false": "Asks about all rows with no restriction.",
            },
        },
        "filter_column": {
            "type": "choice",
            "instructions": "Which column does the main restriction in `question` apply to?",
            "criteria": any_col,
        },
        "filter_operator": {
            "type": "choice",
            "instructions": "How does `question` compare that column?",
            "criteria": COMPARISON_OPS,
        },
        "wants_second_filter": {
            "type": "noul",
            "instructions": "Does `question` apply a SECOND, separate condition on a different column?",
            "criteria": {
                "true": "Two distinct conditions joined by and, such as over 40 AND in Berlin.",
                "false": "Only one condition, or none.",
            },
        },
        "filter2_column": {
            "type": "choice",
            "instructions": "Which column does the second condition apply to?",
            "criteria": any_col,
        },
        "filter2_operator": {
            "type": "choice",
            "instructions": "How does `question` compare that second column?",
            "criteria": COMPARISON_OPS,
        },
        # -- grouping ----------------------------------------------------------
        "group_column": {
            "type": "choice",
            "instructions": "Which column should the breakdown group by?",
            "criteria": any_col,
        },
        "wants_having": {
            "type": "noul",
            "instructions": (
                "Does `question` keep only the categories whose COUNT or TOTAL meets a "
                "threshold, such as 'cities with more than 3 people' or 'products "
                "ordered at least 10 times'? That is a condition on each group, not on "
                "individual rows."
            ),
            "criteria": {
                "true": "The threshold applies to a count or total computed per category.",
                "false": "No condition on the groups, or the condition applies to single rows.",
            },
        },
        "having_operator": {
            "type": "choice",
            "instructions": "How does `question` compare each group's computed value?",
            "criteria": COMPARISON_OPS,
        },
        # -- ordering and slicing ---------------------------------------------
        "wants_order": {
            "type": "noul",
            "instructions": "Does `question` ask for a ranking, sorting, or a top/bottom slice?",
            "criteria": {
                "true": "Says top, highest, lowest, best, worst, most, least, sorted, ranked.",
                "false": "No ordering requested.",
            },
        },
        "order_column": {
            "type": "choice",
            "instructions": "Which column should the ranking sort by?",
            "criteria": _col_criteria(columns, rows, {
                "__agg__": "sort by the aggregate being computed, not a raw column",
                NONE: "no ranking requested",
            }),
        },
        "order_direction": {
            "type": "choice",
            "instructions": "Which end of the ranking does `question` want first?",
            "criteria": {
                "desc": "largest, highest, most, top, latest, best",
                "asc": "smallest, lowest, fewest, bottom, earliest",
            },
        },
        "wants_limit": {
            "type": "noul",
            "instructions": "Does `question` ask for only a fixed number of rows?",
            "criteria": {
                "true": "Says top N, first N, N examples, a handful, just a few.",
                "false": "Wants every matching row.",
            },
        },
        "wants_offset": {
            "type": "noul",
            "instructions": "Does `question` ask to skip the first rows, for example 'the next 10' or 'rows 11-20'?",
            "criteria": {"true": "Asks to skip a number of rows first.", "false": "No skipping."},
        },
        # -- out of scope ------------------------------------------------------
        "needs_judgment": {
            "type": "noul",
            "instructions": (
                "Does `question` ask for something requiring reading and judging free "
                "text — tone, intent, sentiment — rather than an exact comparison?"
            ),
            "criteria": {
                "true": "Asks about anger, confusion, risk of leaving, or similar meaning.",
                "false": "Every condition is an exact comparison a database can evaluate.",
            },
        },
    }

    resp = call({"question": question, "columns": columns, "sample_rows": rows}, q, api_key)
    a = resp["answers"]

    def ch(k):
        v = a[k]["choice"]
        return None if v == NONE else v

    def yes(k):
        return a[k]["noul"] >= 0.5

    shape = a["shape"]["choice"]
    agg = ch("aggregate")
    # The shape decides which slots are real. Reading them all is what produced
    # incoherent SQL before.
    if shape == "rows":
        agg = None
    elif shape == "distinct_values":
        agg = None
    elif shape in ("one_number", "per_category") and not agg:
        agg = "count"

    return {
        "shape": shape,
        "aggregate": agg,
        "agg_column": ch("agg_column"),
        "distinct": shape == "distinct_values",
        "projection": ch("projection"),
        "scalar": ch("scalar"),
        "filters": [
            f for f in (
                {"column": ch("filter_column"), "operator": a["filter_operator"]["choice"]}
                if yes("wants_filter") and ch("filter_column") else None,
                {"column": ch("filter2_column"), "operator": a["filter2_operator"]["choice"]}
                if yes("wants_second_filter") and ch("filter2_column") else None,
            ) if f
        ],
        "group_column": ch("group_column") if shape == "per_category" else None,
        "having": a["having_operator"]["choice"] if yes("wants_having") else None,
        "order": {"column": ch("order_column"), "direction": a["order_direction"]["choice"]}
                 if yes("wants_order") and ch("order_column") else None,
        "limit": yes("wants_limit"),
        "offset": yes("wants_offset"),
        "needs_judgment": a["needs_judgment"]["noul"],
        "numbers": numbers,
        "usage": resp.get("usage", {}),
    }


def resolve_literal(question, column, candidates, api_key):
    """Pick which of the column's real values the question meant. Selection, not
    generation: the literal reaching the SQL is one csvql read out of the file."""
    if not candidates:
        return None
    a = call(
        {"question": question, "column": column, "possible_values": candidates},
        {"value": {
            "type": "choice",
            "instructions": (
                f"`question` filters the column `{column}`. Which of its actual values "
                f"does the question refer to?"
            ),
            "criteria": {v: f"the value {v!r}" for v in candidates[:200]},
        }},
        api_key,
    )
    return a["answers"]["value"]["choice"], a["answers"]["value"].get("confidence")


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def build_sql(csv_path, p, literals=None, numbers=None):
    """Assemble the query. Every fragment is a fixed keyword or a value that came
    from the header or from the file."""
    literals = literals or {}
    nums = list(numbers if numbers is not None else p["numbers"])

    agg, acol = p["aggregate"], p["agg_column"]
    if agg == "count":
        select = "COUNT(*)"
    elif agg and acol:
        select = AGG_SQL[agg].format(c=acol)
    elif p["projection"]:
        col = p["projection"]
        select = SCALAR_SQL[p["scalar"]].format(c=col) if p["scalar"] else col
    else:
        select = "*"

    group = p["group_column"]
    if group and not select.startswith(group):
        select = f"{group}, {select}" if select != "*" else f"{group}, COUNT(*)"

    head = "SELECT DISTINCT " if p["distinct"] and not agg else "SELECT "
    sql = f"{head}{select} FROM '{csv_path}'"

    preds = []
    for i, f in enumerate(p["filters"]):
        col, op = f["column"], f["operator"]
        if op == "is_null":
            preds.append(f"{col} IS NULL")
        elif op == "is_not_null":
            preds.append(f"{col} IS NOT NULL")
        elif op == "between" and len(nums) >= 2:
            preds.append(f"{col} BETWEEN {nums[0]} AND {nums[1]}")
            nums = nums[2:]
        elif op == "contains" and literals.get(i) is not None:
            preds.append(f"{col} LIKE '%{literals[i]}%'")
        elif op == "in_list" and isinstance(literals.get(i), list):
            preds.append(f"{col} IN ({', '.join(_quote(v) for v in literals[i])})")
        elif literals.get(i) is not None:
            preds.append(f"{col} {OP_SQL.get(op, '=')} {_quote(literals[i])}")
        elif nums:
            preds.append(f"{col} {OP_SQL.get(op, '=')} {nums[0]}")
            nums = nums[1:]
    if preds:
        sql += " WHERE " + " AND ".join(preds)

    if group:
        sql += f" GROUP BY {group}"
        if p["having"] and nums:
            sql += f" HAVING COUNT(*) {OP_SQL.get(p['having'], '>')} {nums[0]}"
            nums = nums[1:]

    o = p["order"]
    if o and o["column"]:
        col = select.split(",")[-1].strip() if o["column"] == "__agg__" else o["column"]
        sql += f" ORDER BY {col} {o['direction'].upper()}"

    if p["limit"] and nums:
        sql += f" LIMIT {nums[0]}"
        nums = nums[1:]
        if p["offset"] and nums:
            sql += f" OFFSET {nums[0]}"
    return sql
