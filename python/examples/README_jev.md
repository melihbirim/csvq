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
