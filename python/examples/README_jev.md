# Querying a large CSV in English, for a token cost that does not grow with the file

```
question: "how many enterprise tickets are there per region"   (same 9-column schema in each)

        rows   file size  jev tokens   jev cost    csvql
          10        0.0M       3,322   0.000140    0.00s
     100,000       14.8M       3,322   0.000140    0.01s
  20,000,000     2958.2M       3,322   0.000140    1.12s
```

Three files, a 2,000,000x range of row counts, **the same 3,322 tokens**. The
model is sent the header and three sample rows; it never sees the file. Only
csvql's own runtime moves, because only csvql reads the data.

```python
from jev_grammar import ask

sql, rows, tokens = ask("tickets.csv", "how many enterprise tickets per region")
# SELECT region, COUNT(*) FROM 'tickets.csv' WHERE plan = 'enterprise' GROUP BY region
```

## The model never writes SQL

`jev_grammar.py` owns the grammar. The model only picks which slots to fill,
and every candidate list is built by code first:

| slot | candidates |
|---|---|
| shape | 4 fixed: rows, distinct values, one number, per group |
| column | the real header csvql just read |
| aggregate | 9 fixed names, mapped to SQL templates |
| operator | 10 fixed comparisons |
| string literal | `SELECT DISTINCT <col>` — the column's actual values |
| number | a regex over your question |

Nothing the model returns is concatenated into SQL as text. It cannot name a
column that does not exist, emit an operator csvql cannot run, or invent a
literal absent from the data, so there is no injection surface and no generated
SQL to validate. Pass `run=False` to see the query without executing it.

## Two failures worth reading before you trust it

**Independent questions produced incoherent SQL.** Asking "is it distinct?",
"is it an aggregate?" and "is it grouped?" separately let the code assemble
mutually exclusive shapes at once — `which distinct cities appear` planned
`SELECT city, COUNT(DISTINCT city) GROUP BY city`. They are alternatives, not
dimensions, so they are now a single `shape` Choice that the assembly routes on.

**An instruction collided with a column name.** The grouping slot asked *"which
column is the category?"*, and against a file with a column literally named
`category` it returned that column whatever the question said — so `per region`
grouped by `category`. Reworded to avoid vocabulary likely to appear as a
column name. Worth checking in your own prompts: the schema is adversarial
input to your instructions.

Both were found by running a coverage test, not by reading the code.

## Profiling a file you have never seen (#169)

```console
$ python3 python/examples/jev_profile.py tickets.csv
tickets.csv  2.96 GB  20,000,000 rows  9 columns

  column            type      distinct   empty   range
  ticket_id         int     20,000,000    0.0%   0 .. 19999999
  plan              text             3    0.0%
  region            text             5    0.0%
  replies           int             12    0.0%   0 .. 11

  try (heuristic; set TYPESAFE_API_KEY for better ones):
    csvql "SELECT plan, COUNT(*) FROM 'tickets.csv' GROUP BY plan"
    csvql "SELECT plan, AVG(replies) FROM 'tickets.csv' GROUP BY plan"
```

Everything above the suggestions is pure csvql: **no key, no network**. A key
only upgrades who picks the suggested columns, from a heuristic to the model.
The profile is useful either way, and it falls back to the heuristic if the API
call fails, because a profile that dies on a network error is a bad trade.

The whole thing is **one pass over the file**. The obvious version issues a
query per statistic, which was about thirty full scans and 34 seconds on 2.96 GB;
csvql evaluates any number of aggregates in one `SELECT`, so it is now 5.6s for
identical output.

The heuristics earn their keep by knowing what not to suggest: a column that is
near-unique or named like a key is neither a grouping (one row per row) nor a
measure (the mean of an arbitrary label), so `AVG(ticket_id)` never appears.

## Joining two files

```python
from jev_grammar import join_candidates, ask_join

join_candidates("tickets.csv", "regions.csv")
# [(1.0, 'region', 'region_code')]        100% of sampled values in common

sql, rows, tokens, j = ask_join("tickets.csv", "regions.csv",
                                "show tickets with their region manager")
# SELECT * FROM 'tickets.csv' a JOIN 'regions.csv' b ON a.region = b.region_code
```

The join key is found by csvql, not guessed by the model. Every column pair is
sampled from both files and scored by how much its values actually intersect;
only pairs that overlap survive, and the model picks among joins that
demonstrably work. A pair whose names look alike but whose values never match
scores zero and is never offered.

## Scope

Generated: `SELECT`/`DISTINCT`, `WHERE` with up to two `AND`-ed predicates
(including `BETWEEN`, `IS NULL`, `LIKE`), `GROUP BY`, `HAVING COUNT(*)`,
`ORDER BY`, `LIMIT`, nine aggregates and eight scalar transforms.

Not generated: `JOIN` and multi-file queries, subqueries, `CASE WHEN`, `OR`,
and scalars needing a second argument (`SUBSTR`, `SPLIT_PART`, `REPLACE`,
`CONCAT`, `COALESCE`, `DATEDIFF`, `LPAD`/`RPAD`). csvql supports all of those —
write them by hand with `csvql.query()`.

## This is an example, not part of the package

`pip install csvql-query` installs `query`, `query_csv`, `query_df` and
`query_tuples`, and nothing here. The csvql engine makes no network calls and
has no API key; the only thing that talks to an API is this file, which you run
deliberately.

```sh
export TYPESAFE_API_KEY=...
python3 python/examples/jev_ask.py                        # demo
python3 python/examples/jev_ask.py big.csv "top 5 cities" # your own file
```
