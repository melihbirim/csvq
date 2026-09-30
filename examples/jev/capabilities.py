"""What csvql can actually do, read out of csvql's own source.

The planner's grammar has to track the engine. Hand-maintaining a list of
supported functions guarantees drift: LPAD and RPAD landed in #178 and would
have been missing from a list written the week before.

So the aggregate and scalar function names come from the Zig enums that the
engine itself switches on:

    src/aggregation.zig   AggregateType      -> the aggregates
    src/scalar.zig        ScalarSpec         -> the scalar functions

`test_drift.py` fails when those grow something this module has no description
for, which turns "we are going to add new SQL functions" from a silent
regression into a failing test.

Descriptions live here because the model needs plain English, not identifiers:
`stddev` has to read as "the spread or variability" for a question that says
"how consistent are the prices". Adding a function means adding one line here.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Read the engine's own enums.
# ---------------------------------------------------------------------------

def _zig_enum_members(path, decl, terminator="};"):
    src = (REPO / path).read_text()
    start = src.index(decl)
    body = src[start:src.index(terminator, start)]
    return [
        m.group(1)
        for line in body.splitlines()[1:]
        if (m := re.match(r"\s{4}([a-z_][a-z0-9_]*)\s*[,:]", line))
    ]


def engine_aggregates():
    return _zig_enum_members("src/aggregation.zig", "pub const AggregateType = enum {")


def engine_scalars():
    return _zig_enum_members("src/scalar.zig", "pub const ScalarSpec = union(enum) {")


# ---------------------------------------------------------------------------
# Plain-English descriptions, keyed by the engine's own identifiers.
# ---------------------------------------------------------------------------

AGGREGATE_DESC = {
    "count": "how many rows",
    "count_distinct": "how many different values a column has",
    "sum": "the total of a numeric column",
    "avg": "the average, mean or typical value",
    "min": "the smallest, earliest or cheapest value",
    "max": "the largest, latest or most expensive value",
    "median": "the middle or typical value, robust to outliers",
    "variance": "the variance across the whole population",
    "stddev": "the spread across the whole population",
    "variance_samp": "the variance, treating the rows as a sample",
    "stddev_samp": "the spread or variability, treating the rows as a sample",
    "group_concat": "every value joined together into one list",
}

AGGREGATE_SQL = {
    "count": "COUNT(*)",
    "count_distinct": "COUNT(DISTINCT {c})",
    "sum": "SUM({c})",
    "avg": "AVG({c})",
    "min": "MIN({c})",
    "max": "MAX({c})",
    "median": "MEDIAN({c})",
    "variance": "VAR_POP({c})",
    "stddev": "STDDEV_POP({c})",
    "variance_samp": "VARIANCE({c})",
    "stddev_samp": "STDDEV({c})",
    "group_concat": "GROUP_CONCAT({c})",
}

# Scalars the planner can place in a SELECT list. Engine members deliberately
# left out are listed in SCALARS_NOT_PLANNED with the reason, so the drift test
# can tell "new and unhandled" from "known and intentionally skipped".
SCALAR_DESC = {
    "upper": "in capitals, uppercased",
    "lower": "in lower case",
    "trim": "with surrounding whitespace removed",
    "reverse": "with the characters reversed",
    "length": "how long the text is, in characters",
    "abs": "the size ignoring any minus sign",
    "sign": "whether it is positive, negative or zero",
    "ceil": "rounded up to a whole number",
    "floor": "rounded down to a whole number",
    "round_op": "rounded to a whole number",
    "cast_int": "as a whole number",
    "cast_float": "as a decimal number",
    "cast_text": "as text",
    "strftime": "just one part of the date, such as the year or the month",
    "extract": "a single named part of a date, such as the year",
    "substr": "only the first few characters",
    "split_part": "one field out of a delimited value",
    "lpad": "padded on the left to a fixed width",
    "rpad": "padded on the right to a fixed width",
    "replace": "with one piece of text swapped for another",
    "concat": "joined together with another column",
    "coalesce": "falling back to another column when empty",
    "datediff": "the gap between two dates",
    "dateadd": "shifted forwards or backwards in time",
    "greatest": "the larger of several columns",
    "least": "the smaller of several columns",
    "mod_op": "the remainder after dividing",
    "is_null_check": "whether the value is missing",
}

# Single-argument scalars the planner can emit unaided. The rest need a second
# argument (a format, a delimiter, a width) that the grammar has no slot for.
SCALAR_SQL = {
    "upper": "UPPER({c})",
    "lower": "LOWER({c})",
    "trim": "TRIM({c})",
    "reverse": "REVERSE({c})",
    "length": "LENGTH({c})",
    "abs": "ABS({c})",
    "sign": "SIGN({c})",
    "ceil": "CEIL({c})",
    "floor": "FLOOR({c})",
    "round_op": "ROUND({c}, 0)",
    "cast_int": "CAST({c} AS INT)",
    "cast_float": "CAST({c} AS FLOAT)",
    "cast_text": "CAST({c} AS TEXT)",
}

SCALARS_NOT_PLANNED = {
    "nested": "a composition of two other functions, not a function itself",
    "case_when": "needs branches the grammar has no slots for",
    "substr": "needs start and length arguments",
    "split_part": "needs a delimiter and a field index",
    "lpad": "needs a target width",
    "rpad": "needs a target width",
    "replace": "needs both the search and the replacement text",
    "concat": "needs a second column or literal",
    "coalesce": "needs a fallback column",
    "datediff": "needs a unit and a second date column",
    "dateadd": "needs a unit and an amount",
    "greatest": "needs at least two columns",
    "least": "needs at least two columns",
    "mod_op": "needs a divisor",
    "is_null_check": "expressed as an IS NULL filter instead",
    "strftime": "emitted through the date-part slots, not as a bare scalar",
    "extract": "emitted through the date-part slots, not as a bare scalar",
}

# Date parts, which the planner emits as STRFTIME with a fixed format string.
DATE_PARTS = {
    "year": ("just the year", "STRFTIME('%Y', {c})"),
    "month": ("the year and month", "STRFTIME('%Y-%m', {c})"),
    "day": ("the calendar date", "STRFTIME('%Y-%m-%d', {c})"),
}

# Clause-level features. JOIN is real in the engine (INNER JOIN, and multiple
# files) and is planned separately, because it needs a second file and a join
# key rather than a slot in the single-file grammar.
CLAUSES = [
    "select", "distinct", "where", "and", "or", "not", "between", "in",
    "is_null", "like", "ilike", "group_by", "having", "order_by", "limit",
    "offset", "join",
]


def planner_aggregates():
    """Engine aggregates the planner can offer, in engine order."""
    return [a for a in engine_aggregates() if a in AGGREGATE_SQL]


def planner_scalars():
    return [s for s in engine_scalars() if s in SCALAR_SQL]


def undescribed():
    """Engine functions with no description here. Non-empty means drift."""
    return {
        "aggregates": [a for a in engine_aggregates() if a not in AGGREGATE_DESC],
        "scalars": [
            s for s in engine_scalars()
            if s not in SCALAR_DESC and s not in SCALARS_NOT_PLANNED
        ],
    }


if __name__ == "__main__":
    print(f"engine aggregates ({len(engine_aggregates())}): {' '.join(engine_aggregates())}")
    print(f"  planner can emit ({len(planner_aggregates())}): {' '.join(planner_aggregates())}")
    print(f"engine scalars ({len(engine_scalars())}): {' '.join(engine_scalars())}")
    print(f"  planner can emit ({len(planner_scalars())}): {' '.join(planner_scalars())}")
    d = undescribed()
    print(f"undescribed: {d}" if any(d.values()) else "undescribed: none — grammar is in sync")
