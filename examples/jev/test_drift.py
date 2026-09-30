#!/usr/bin/env python3
"""Fail when csvql grows a SQL function the planner knows nothing about.

Run it in CI. Adding a scalar or aggregate to the engine makes this fail until
someone either writes a plain-English description for it or records, in
SCALARS_NOT_PLANNED, why the planner cannot emit it.

Needs no API key and no network: it reads the Zig source and the Python
grammar and compares them.

    python3 test_drift.py
"""

import sys

import capabilities as cap


def main():
    failures = []

    missing = cap.undescribed()
    if missing["aggregates"]:
        failures.append(
            f"aggregates in src/aggregation.zig with no description in "
            f"capabilities.py: {', '.join(missing['aggregates'])}\n"
            f"    add each to AGGREGATE_DESC and AGGREGATE_SQL so the planner can offer it."
        )
    if missing["scalars"]:
        failures.append(
            f"scalars in src/scalar.zig with no description in capabilities.py: "
            f"{', '.join(missing['scalars'])}\n"
            f"    add each to SCALAR_DESC (+ SCALAR_SQL if it takes one argument), or to\n"
            f"    SCALARS_NOT_PLANNED with the reason it cannot be planned."
        )

    # Anything the planner claims to emit must have both a description and a
    # template, or it will produce SQL with a hole in it at runtime.
    for a in cap.planner_aggregates():
        if a not in cap.AGGREGATE_DESC:
            failures.append(f"aggregate {a!r} has SQL but no description")
    for s in cap.planner_scalars():
        if s not in cap.SCALAR_DESC:
            failures.append(f"scalar {s!r} has SQL but no description")

    # And nothing may be both emittable and declared unplannable.
    both = set(cap.SCALAR_SQL) & set(cap.SCALARS_NOT_PLANNED)
    if both:
        failures.append(
            f"scalars in both SCALAR_SQL and SCALARS_NOT_PLANNED: {', '.join(sorted(both))}"
        )

    # Templates must have the substitution the builder will format.
    for name, tmpl in cap.AGGREGATE_SQL.items():
        if name != "count" and "{c}" not in tmpl:
            failures.append(f"aggregate template {name!r} has no {{c}} placeholder")
    for name, tmpl in cap.SCALAR_SQL.items():
        if "{c}" not in tmpl:
            failures.append(f"scalar template {name!r} has no {{c}} placeholder")

    n_agg, n_scalar = len(cap.engine_aggregates()), len(cap.engine_scalars())
    if failures:
        print(f"DRIFT: the planner is out of sync with the engine "
              f"({n_agg} aggregates, {n_scalar} scalars in source)\n")
        for f in failures:
            print(f"  - {f}")
        print("\nSee examples/jev/capabilities.py.")
        return 1

    print(f"in sync: {n_agg} engine aggregates "
          f"({len(cap.planner_aggregates())} plannable), "
          f"{n_scalar} engine scalars ({len(cap.planner_scalars())} plannable, "
          f"{len(cap.SCALARS_NOT_PLANNED)} explicitly not planned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
