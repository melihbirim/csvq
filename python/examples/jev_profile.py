#!/usr/bin/env python3
"""Profile a CSV: shape, per-column types and null rates, and queries to try.

    python3 python/examples/jev_profile.py data.csv

Everything except the last section is pure csvql and needs no API key and no
network. Set TYPESAFE_API_KEY and the suggestions are chosen by the model
instead of by heuristics; without it you still get suggestions, just blunter
ones. The profile is useful either way, which is the point: a key is an
upgrade, not a dependency.

This is the Jev-facing half of issue #169. The other half, `csvql data.csv`
printing a summary from the binary itself, is a Zig change and stays in the
engine where it needs no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import csvql  # noqa: E402


def _scalar(sql):
    rows = csvql.query(sql)
    return list(rows[0].values())[0] if rows else None


def infer_type(path, col, sample):
    """int / float / date / text, from a sampled value plus a cheap check."""
    v = str(sample).strip()
    if not v:
        return "empty"
    if v.lstrip("-").isdigit():
        return "int"
    try:
        float(v)
        return "float"
    except ValueError:
        pass
    if len(v) >= 8 and v[:4].isdigit() and v[4] in "-/":
        return "date"
    return "text"


def profile(path):
    """Every statistic for every column, in a single pass over the file.

    The obvious implementation issues one query per statistic, which on a 2.96 GB
    file meant about thirty full scans and 34 seconds. csvql evaluates any number
    of aggregates in one SELECT, so the whole profile is one scan: 2.3 seconds on
    the same file. Reading a large CSV once instead of thirty times is the entire
    optimisation.
    """
    head = csvql.query(f"SELECT * FROM '{path}' LIMIT 1")
    if not head:
        raise SystemExit(f"{path} has no rows")
    columns = list(head[0].keys())
    kinds = {c: infer_type(path, c, head[0][c]) for c in columns}

    parts = ["COUNT(*)"]
    for c in columns:
        # COUNT(col) skips empty fields, so total minus this is the null count.
        parts += [f"COUNT({c})", f"COUNT(DISTINCT {c})"]
        if kinds[c] in ("int", "float"):
            parts += [f"MIN({c})", f"MAX({c})"]
    row = csvql.query(f"SELECT {', '.join(parts)} FROM '{path}'")[0]
    vals = list(row.values())

    total, i = vals[0], 1
    stats = []
    for c in columns:
        non_null, distinct, i = vals[i] or 0, vals[i + 1] or 0, i + 2
        s = {"name": c, "type": kinds[c], "distinct": distinct,
             "null_pct": 100.0 * (total - non_null) / total if total else 0.0}
        if kinds[c] in ("int", "float"):
            s["min"], s["max"], i = vals[i], vals[i + 1], i + 2
        stats.append(s)
    return {"path": path, "rows": total, "columns": stats}


def _looks_like_id(s, rows):
    """Near-unique, or named like a key. Grouping by one gives a row per row, and
    averaging one is arithmetic on an arbitrary label."""
    name = s["name"].lower()
    return (s["distinct"] >= rows * 0.9
            or name in ("id", "key", "uuid", "guid")
            or name.endswith(("_id", "_key", "_uuid")))


def _category_like(stats, rows):
    """Columns worth grouping by: repeating values, and not an identifier."""
    return [s for s in stats
            if s["type"] in ("text", "int", "date")
            and 1 < s["distinct"] <= max(50, rows * 0.01)
            and not _looks_like_id(s, rows)]


def _measure_like(stats, rows):
    """Numeric columns worth averaging. Identifiers are numeric too, and their
    mean means nothing, so they are excluded by name and by uniqueness."""
    return [s for s in stats
            if s["type"] in ("int", "float")
            and s["distinct"] > 1
            and not _looks_like_id(s, rows)]


def suggest_heuristic(p):
    """Suggestions with no model involved. Deliberately the fallback, and
    deliberately still useful."""
    stats, rows, path = p["columns"], p["rows"], p["path"]
    cats, nums = _category_like(stats, rows), _measure_like(stats, rows)
    out = [f"SELECT COUNT(*) FROM '{path}'"]
    if cats:
        out.append(f"SELECT {cats[0]['name']}, COUNT(*) FROM '{path}' "
                   f"GROUP BY {cats[0]['name']}")
    if cats and nums:
        out.append(f"SELECT {cats[0]['name']}, AVG({nums[0]['name']}) FROM '{path}' "
                   f"GROUP BY {cats[0]['name']}")
    elif nums:
        out.append(f"SELECT MIN({nums[0]['name']}), MAX({nums[0]['name']}), "
                   f"AVG({nums[0]['name']}) FROM '{path}'")
    worst = max(stats, key=lambda s: s["null_pct"])
    if worst["null_pct"] > 5:
        out.append(f"SELECT COUNT(*) FROM '{path}' WHERE {worst['name']} IS NULL")
    return out


def suggest_with_jev(p, api_key):
    """Let the model pick which columns are worth looking at.

    Two Choices over the real columns, so the worst case is a boring but valid
    query rather than a broken one. Falls back to the heuristics on any error:
    a profile that dies because an API call failed would be a bad trade.
    """
    from jev_grammar import _call  # noqa: PLC0415

    stats, path, rows = p["columns"], p["path"], p["rows"]
    cats, nums = _category_like(stats, rows), _measure_like(stats, rows)
    if not cats:
        return suggest_heuristic(p)

    def crit(items):
        return {s["name"]: f"{s['type']}, {s['distinct']} distinct values, "
                           f"{s['null_pct']:.0f}% empty" for s in items}

    questions = {
        "grouping": {
            "type": "choice",
            "instructions": ("Someone has just opened this file and wants one query that "
                             "shows them what is in it. Which column's breakdown would be "
                             "most informative to see counted?"),
            "criteria": crit(cats),
        }
    }
    if nums:
        questions["measure"] = {
            "type": "choice",
            "instructions": "Which numeric column is most likely the one people care about?",
            "criteria": crit(nums),
        }
    try:
        a = _call({"file": os.path.basename(path), "row_count": rows,
                   "columns": stats}, questions, api_key)["answers"]
    except Exception:
        return suggest_heuristic(p)

    g = a["grouping"]["choice"]
    out = [f"SELECT COUNT(*) FROM '{path}'",
           f"SELECT {g}, COUNT(*) FROM '{path}' GROUP BY {g} ORDER BY COUNT(*) DESC"]
    if "measure" in a:
        m = a["measure"]["choice"]
        out.append(f"SELECT {g}, AVG({m}), MAX({m}) FROM '{path}' GROUP BY {g}")
    return out


def render(p, api_key=None):
    size = os.path.getsize(p["path"])
    unit = f"{size / 1e9:.2f} GB" if size > 1e9 else f"{size / 1e6:.1f} MB"
    print(f"{p['path']}  {unit}  {p['rows']:,} rows  {len(p['columns'])} columns\n")
    print(f"  {'column':<18}{'type':<8}{'distinct':>10}{'empty':>8}   range")
    for s in p["columns"]:
        rng = f"{s['min']} .. {s['max']}" if "min" in s else ""
        print(f"  {s['name']:<18}{s['type']:<8}{s['distinct']:>10,}"
              f"{s['null_pct']:>7.1f}%   {rng}")

    suggestions = suggest_with_jev(p, api_key) if api_key else suggest_heuristic(p)
    how = "chosen by jev" if api_key else "heuristic; set TYPESAFE_API_KEY for better ones"
    print(f"\n  try ({how}):")
    for q in suggestions:
        print(f"    csvql \"{q}\"")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip().split("\n\n")[1])
    render(profile(sys.argv[1]), os.environ.get("TYPESAFE_API_KEY"))


if __name__ == "__main__":
    main()
