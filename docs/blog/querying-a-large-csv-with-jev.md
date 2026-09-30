---
layout: post
title: "Querying a 3 GB CSV with Jev Costs the Same as Querying 10 Rows"
description: "Jev turns an English question into SQL for a CSV without ever seeing the data. One question, files from 10 to 20 million rows, the same 3,322 tokens every time."
date: 2026-09-30
---

*The usual way to query a CSV with a language model is to send it rows, so the cost scales with the file. Send it the schema instead and the cost stops moving. Here is the measurement, the trick that makes it safe, and the two bugs that took a working version and made it a correct one.*

---

## The measurement

One question. Three files with an identical nine column schema, from 10 rows to 20 million.

```
question: "how many enterprise tickets are there per region"

        rows   file size  jev tokens   jev cost    csvql
          10        0.0M       3,322   0.000140    0.00s
     100,000       14.8M       3,322   0.000140    0.01s
  20,000,000     2958.2M       3,322   0.000140    1.12s
```

A 2,000,000x range of row counts and the token count does not move by one token. The only column that changes is csvql's own runtime, because csvql is the only thing that reads the data.

The reason is dull and that is the point: the model is sent the header and three sample rows. Not a page of rows, not a sample that grows with the file. Three. It never sees the data, so there is nothing in the payload for the file size to inflate.

The question it answers is a real one:

```sql
SELECT region, COUNT(*) FROM 'tickets.csv'
WHERE plan = 'enterprise' GROUP BY region
```

That query was not written by a human and not written by the model either.

---

## The model never writes SQL

This is the part that matters, and it is a constraint rather than a safeguard bolted on afterwards.

[Jev](https://docs.typesafe.ai) is a System One model: it returns typed judgments, not text. Ask it a Choice question and you get back one of the options you gave it, with a probability. It has no mechanism for emitting a string you did not already write down.

So the SQL is assembled by code from a fixed grammar, and the model only chooses which slots to fill:

| slot | candidates come from |
|---|---|
| shape | four fixed options: rows, distinct values, one number, one row per group |
| column | the real header, which csvql just read off the file |
| aggregate | nine fixed names, each mapped to a SQL template |
| operator | ten fixed comparisons |
| string literal | `SELECT DISTINCT <col>`, the column's actual values |
| number | a regex over your question |

Every list is built before the model is asked anything. It cannot name a column that does not exist, produce an operator the engine cannot run, or invent a literal that is not in the data, because none of those are on the menu.

The consequence is that there is no generated string anywhere in the pipeline. Nothing to sanitize, no SQL to validate afterwards, no injection surface, because at no point does model output get concatenated into a query. The literal in `WHERE plan = 'enterprise'` is a value csvql read out of the file a moment earlier.

That is a different safety story from "we asked it for SQL and then checked the SQL". Checking is a filter you can get wrong. This is a shape that cannot express the bad case.

---

## Two bugs, both found by running it

The first version worked, in the sense that it produced SQL and the SQL ran. Both of these were caught by a test that asks one question per feature and checks the result, not by reading the code.

### Independent questions assembled contradictory queries

The first design asked three separate yes/no questions: is this a DISTINCT query, is this an aggregate, is this grouped. Code then assembled whatever came back.

`which distinct cities appear` planned this:

```sql
SELECT city, COUNT(DISTINCT city) FROM 'people.csv' GROUP BY city
```

Every individual judgment is defensible. The combination is nonsense. The three are not independent dimensions, they are mutually exclusive shapes, and asking about them separately gave the code permission to pick all of them at once.

Replacing them with a single Choice over four shapes, and having the assembly read only the slots that shape needs, fixed that case and two others with it.

There is a general rule in here. Decomposing a judgment into smaller judgments is usually right, and the [docs recommend it](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md). But splitting alternatives into parallel booleans is not decomposition, it is discarding the constraint that exactly one of them is true.

### An instruction collided with a column name

The grouping slot asked, in plain English:

> Which column is the category?

Against a support ticket file it grouped by `category` every single time. `how many enterprise tickets per region` grouped by `category`. The file has a column literally named `category`, and the instruction pointed straight at it.

This one is worth sitting with, because the code was fine. The bug was a word. The user's schema is untrusted input against your prompt text, in exactly the way user data is untrusted input against your SQL, and a column can be named anything at all. Reworded to avoid vocabulary likely to appear as a column name, it now groups by `region` when asked for regions and still by `category` when asked for categories.

If you write this kind of slot filling, read your instructions once more imagining the worst possible schema. A file with columns called `value`, `column`, `count` and `group` is not hypothetical.

---

## What this does not do

The grammar generates `SELECT` and `DISTINCT`, `WHERE` with up to two `AND`ed predicates including `BETWEEN`, `IS NULL` and `LIKE`, `GROUP BY`, `HAVING COUNT(*)`, `ORDER BY`, `LIMIT`, nine aggregates and eight scalar transforms.

It does not generate `JOIN` or multi file queries, subqueries, `CASE WHEN`, `OR`, or the scalar functions that need a second argument such as `SUBSTR`, `REPLACE` and `SPLIT_PART`. csvql runs all of those. The planner does not write them, and widening it means adding templates, not a better model.

It also cannot answer a question that needs the text to be read rather than compared. "Which customers sound angry" is not a SQL predicate, and the planner says so rather than guessing. That is a different technique with a different cost profile: every row goes to the model, and the bill scales with the file again.

---

## Keeping the grammar honest as the engine grows

csvql gains SQL functions. `LPAD` and `RPAD` landed a week before this was written. A hand maintained list of what the planner supports would have been wrong immediately and silently.

So the aggregate and scalar lists are read out of csvql's own Zig enums, and a test fails when the engine grows something the planner has no description for:

```console
$ python3 test_drift.py
in sync: 12 engine aggregates (12 plannable), 30 engine scalars
         (13 plannable, 17 explicitly not planned)
```

Adding a function to the engine turns into a failing test asking for one line of plain English, rather than a capability that quietly never gets offered. The test needs no API key and no network, so it runs in CI with everything else.

---

## How this compares to putting it in SQL

DuckDB shipped a [jev community extension](https://duckdb.org/2026/09/29/jev) the day before this post, which puts plain English conditions inside SQL as `WHERE jev('the customer is angry', body)`.

It is a good design and it is more powerful than what is described here. Because it is a boolean function, it composes with the whole language: you can group by a judgment, sort by one, join on a filtered set. This grammar fills one template and cannot do any of that. It also already runs cheap predicates first, so rows a `WHERE age > 40` rejects never reach the API.

The difference is what gets sent. In the extension, rows go to the model, batched, and the planner decides which. Here the model sees the header and three rows and never the data, which is what makes the token cost flat. Two different tools: one for a semantic predicate you want to compose with SQL, one for turning a question into SQL over a file you would rather not send anywhere.

If your data is already in DuckDB and you are permitted to send it to a third party, the extension is the shorter path.

---

## Caveats

- **The token and timing numbers are measured**, on an M2 Pro, against the fixture generated by the script in the example directory. The cost column is those measured token counts times TypeSafe's published $0.042 per million input tokens, not a bill anyone received.
- **Coverage is not complete and not exhaustively re-measured** on the final version. Two specific failures were found, understood and fixed; that is not the same as proving the rest correct. Use `run=False` to see the SQL before you trust a query you have not checked.
- **Accuracy claims about the model are out of scope here.** This post is about token cost and query construction. Nothing in it measures how often Jev picks the right column on schemas other than these.
- **Jev is early access** and priced at list rates that may change.

---

The example is in [`python/examples/`](https://github.com/melihbirim/csvql/tree/main/python/examples), stdlib only. It is an example rather than a feature: `pip install csvql-query` installs `query`, `query_csv`, `query_df` and `query_tuples` and nothing else, and the csvql engine makes no network calls and holds no API key. The only thing here that talks to an API is a file you run on purpose.
