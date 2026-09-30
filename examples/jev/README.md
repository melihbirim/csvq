# English questions over a CSV, without sending the data anywhere

```console
$ csvql-ask people.csv "total age by city, highest first"
sql      SELECT city, SUM(age) FROM 'people.csv' GROUP BY city ORDER BY SUM(age) DESC
cost     3,512 input tokens, $0.000148 at list price, 0 rows of data sent
```

The model gets the **header and five sample rows**. Nothing else. It never sees
the file, and it never writes SQL.

## How the SQL gets written

Code owns a grammar. The model fills slots in it, and every slot is a Choice
over candidates code produced first:

| slot | candidates come from |
|---|---|
| shape (rows / distinct / one number / per category) | 4 fixed options |
| columns | the real header csvql just read |
| aggregate | `src/aggregation.zig`'s `AggregateType` enum |
| scalar | `src/scalar.zig`'s `ScalarSpec` union |
| operator | 6 fixed comparisons |
| string literal | `SELECT DISTINCT <col>` — the column's actual values |
| numbers | a regex over your question |

So the model cannot name a column that does not exist, an operator csvql cannot
run, or a literal that is not in the file. There is no generated fragment to
sanitize and no SQL to validate afterwards. `--explain` prints the query and
stops.

## Keeping up with the engine

`capabilities.py` reads the aggregate and scalar lists out of csvql's own Zig
enums rather than repeating them. `test_drift.py` fails when the engine grows a
function nothing here describes:

```console
$ python3 test_drift.py
in sync: 12 engine aggregates (12 plannable), 30 engine scalars
         (13 plannable, 17 explicitly not planned)
```

Adding a SQL function to csvql makes that test fail until someone writes one
line of plain English for it — `stddev` has to read as "the spread or
variability" for a question like "how consistent are the prices". A function
that cannot be planned (it needs a second argument the grammar has no slot for)
is recorded in `SCALARS_NOT_PLANNED` with the reason, so "new and unhandled" is
distinguishable from "known and skipped". No API key or network needed.

## Measured coverage

`coverage_test.py` asks one question per feature, checks the planned SQL against
required **and forbidden** fragments, then runs it through csvql.

```console
$ python3 coverage_test.py people.csv
18/20 planned and ran correctly, 70,249 input tokens, $0.00295
```

The forbidden list matters. An earlier version matched substrings only and
scored 19/20 while `"which distinct cities appear"` planned
`SELECT city, COUNT(DISTINCT city) GROUP BY city` — a wrong query that contains
the string `DISTINCT`. Assert what must not appear, or the test flatters you.

**Known failures, both shape misclassification:**

| question | planned | should be |
|---|---|---|
| `how long is each review` | `SELECT COUNT(*)` | `SELECT LENGTH(review)` |
| `cities with more than 3 people` | `SELECT DISTINCT city WHERE city > 'London'` | `... GROUP BY city HAVING COUNT(*) > 3` |

Both pick the wrong shape, then fill slots consistently with that wrong shape.
The fix is better shape criteria, not more slots.

## Not generated

JOIN and multi-file queries, subqueries, `CASE WHEN`, `OR`, arithmetic in the
SELECT list, and nested scalar calls. csvql supports all of these — the planner
does not, and `SCALARS_NOT_PLANNED` lists the per-function reasons. Write those
by hand.

## Files

| | |
|---|---|
| `csvql-ask` | the CLI |
| `plan.py` | slot questions, one request per query, SQL assembly |
| `capabilities.py` | reads csvql's Zig enums; plain-English descriptions |
| `test_drift.py` | fails when the engine outgrows the grammar (no network) |
| `coverage_test.py` | one question per feature, measured (needs a key) |
