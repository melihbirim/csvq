# Semantic filtering over a CSV too large to send anywhere

Some questions are part SQL and part judgment:

> Which enterprise billing tickets from the last quarter are from **angry** customers
> **at risk of churning**?

`enterprise`, `billing` and the date range are exact predicates. `angry` and
`at risk of churning` are not — no `LIKE` pattern decides them, because the angry
and the calm tickets in this dataset share most of their vocabulary.

The working split is: **exact predicates in SQL, judgments in a model, in that
order.** csvql scans the raw CSV locally and hands the surviving 2,785 rows to
[TypeSafe's Jev](https://docs.typesafe.ai), which returns calibrated probabilities
rather than generated text.

```
      2.96 GB CSV                 2,785 rows                    the answer
   ───────────────────►  csvql  ───────────────►   Jev   ───────────────►
     20,000,000 rows             1.2s, one scan,        3 judgments per row,
                                 no network             0.009% of the file
                                                        leaves the machine
```

## Ask in English: `csvql-ask`

```console
$ csvql-ask people.csv "give me the people who are angry over 40"
plan  sql:      SELECT * FROM 'people.csv' WHERE age > 40
      judge:    'review' against the subjective part (text column confidence 1.00)

stage 1  csvql -> 25 rows
stage 2  Jev judged 25 rows, 10,164 input tokens, $0.00043 at list price

9 match at p >= 0.7:
  p=0.95  name=Ada20, age=54, city=Berlin
          Your support team closed my ticket without fixing anything. Unbelievable.
  ...
```

No SQL. `over 40` became a `WHERE` clause; `angry` became a judgment; the split
was decided by the model and printed before anything ran. `--explain` stops
after the plan.

**Jev never writes the SQL.** It returns typed judgments, not text, so the plan
is assembled by selection: code supplies the candidates — the real column names,
the numeric literals found in the question, the five operators csvql implements —
and Jev picks which ones the question meant. The model cannot name a column that
does not exist or an operator the engine cannot run, because neither is on the
menu. That is the [select, don't generate](https://docs.typesafe.ai/cookbooks/pre_parsed_value_extraction_cookbook.md)
pattern, and it is why this needs no SQL validation layer.

The whole plan is one request: the schema goes out once and the questions run in
parallel, including the speculative ones about a numeric filter that a question
without numbers simply never consumes.

## Running the two-stage script directly

```sh
export TYPESAFE_API_KEY=...
./semantic_filter.py tickets.csv

./semantic_filter.py tickets.csv --dry-run    # stage 1 only, no key needed
./semantic_filter.py tickets.csv --limit 100  # cap rows sent to the model
```

Needs `csvql` on `PATH` (or `CSVQL=/path/to/csvql`). No Python dependencies —
stdlib only, so there is nothing to install and nothing to audit.

Generate the fixture with `gen_tickets.py` (deterministic; 20M rows ≈ 2.96 GB).

## Why the order matters

Judging every row is the obvious approach and the wrong one. Each request here
carries **589 input tokens**, measured from the API's own `usage` field — the
ticket plus the instructions and criteria, which are resent every time. At Jev's
published $0.042 per million input tokens, with output tokens free:

| | rows judged | input tokens | cost at list price |
|---|---|---|---|
| judge everything | 20,000,000 | 11.8 billion | **~$495** |
| filter first, then judge | 2,785 | 1.6 million | **~$0.07** |

Same answer, roughly 7,000x less money, because the SQL predicates are exact and
free and they remove 99.986% of the rows before anything is paid for. The scan
that does it takes 1.2 seconds.

The privacy arithmetic runs the same way. The full file never leaves the machine;
only the surviving rows do. The script prints exactly how many bytes that was.

## Three judgments, one request

The three questions are independent and about the same ticket, so they go in a
single request. The [docs are explicit](https://docs.typesafe.ai/cookbooks/parallel_questions.md)
that this is the right shape: the questions run in parallel and the ticket text is
sent once rather than three times. Splitting them into three calls would roughly
triple the input tokens for the same answers.

```python
"questions": {
    "angry":       {"type": "noul",   ...},   # probability, 0.0 - 1.0
    "churn_risk":  {"type": "noul",   ...},
    "next_action": {"type": "choice", ...},   # escalate / reply / no action
}
```

`Noul` returns the probability that a condition holds. It is not a confidence
score: 0.5 means genuinely balanced evidence, not "medium anger". The threshold
that turns those probabilities into a decision lives in the script, not in the
model, so changing the policy does not mean re-judging anything.

## How this compares to doing it in SQL

DuckDB's [jev extension](https://duckdb.org/2026/09/29/jev) puts the same
capability inside SQL, as `WHERE jev('the customer is angry', body)`. It is a
good design and it already does the important thing: combine it with a cheap
predicate and the SQL optimizer runs the cheap one first, so non-matching rows
never reach the API. **The prefilter idea is not what separates these two.**

What actually differs, measured on this fixture, best of 3:

| stage 1 query | csvql | DuckDB |
|---|---|---|
| `COUNT(*)` with the same `WHERE` | 0.92s / **53 MB** | 0.94s / 247 MB |
| the real projection, 7 columns incl. `body` | 1.27s / **492 MB** | **1.15s** / 764 MB |

**Speed is a wash on this workload, and DuckDB is marginally ahead once the text
column is projected.** That is worth stating plainly, because csvql's published
benchmarks show a wider gap on other shapes — 1.95s against 4.34s for a
single-predicate `COUNT(*)` on an 11 GB file. Those numbers do not carry over
here: this query has five predicates and a long quoted text column, and quoted
field parsing is where the gap closes. Measure your own shape rather than
inheriting either number.

The consistent difference is memory: 4.7x less on the scan, 1.55x less once the
body column is materialized. On a small box next to the data, that is the part
that decides whether the job runs at all.

The other two differences are architectural rather than benchmarkable:

- **The network call is outside the engine.** csvql makes no network calls and
  has no API key. The model call happens in this script, where you can read it,
  log it, rate-limit it, or replace it. With an in-engine extension, which rows
  leave the machine is decided by the query planner. Run stage 1 under `--root`
  and the scan is sandboxed with stage 2 as the only egress point, by construction.
- **Nothing to install.** The extension needs DuckDB v1.5.5 specifically and is
  unsigned at the time of writing.

If your data is already in DuckDB and you are allowed to send it to a third
party, the extension is the shorter path and probably the right one. This example
is for the case where the data is a raw CSV, memory is tight, or you want the
egress boundary to be something you wrote rather than something a planner chose.

## Caveats worth reading before you trust the numbers

- **Only stage 1 is measured here.** The 2.96 GB scan and row counts are real,
  from the fixture in this directory. The cost table is arithmetic from Jev's
  published list price and measured token counts per row, not a bill anyone paid.
- **Accuracy was measured, on an easy task.** Both fixtures are generated, so
  every row carries a ground-truth label. On a balanced 40-ticket sample Jev
  scored 100% at every threshold from 0.5 to 0.9, with the probabilities well
  separated: angry rows 0.92-0.98, calm rows 0.04-0.08. `csvql-ask` returned
  exactly the 9 angry people over 40, with no false positives or negatives.
  Read that as evidence the plumbing is right, not that the model is infallible:
  eight fixed bodies with unambiguous tone is a much easier problem than real
  support mail. Label your own data before picking a threshold.
- **The fixture is synthetic**, with four angry and four calm bodies. It is built
  so that keyword matching cannot substitute for judgment, which makes it a fair
  demonstration of the pattern and a poor benchmark of the model.
- **One request per row** is the accurate default. Batching several rows into one
  request amortizes the instruction tokens, but the DuckDB extension authors
  report accuracy dropping above roughly 20-25 rows per request.
