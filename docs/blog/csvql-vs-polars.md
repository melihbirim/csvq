---
layout: post
title: "csvql vs polars: The Memory Stays Flat"
description: "polars is what people move to when pandas gets slow, and it is genuinely fast. On the same queries csvql is 1.5 to 3.2x ahead of it at a flat 75 MB of memory, against polars settling around 2 GB. Includes the extrapolation we got wrong."
date: 2026-10-05
---

Beating pandas is not an interesting claim. pandas loads the whole CSV into a DataFrame before it will answer anything, so any engine that doesn't do that wins by a margin that says more about the comparison than the engine. We [wrote that post](csvql-vs-pandas.html) and the number was 17 to 76x.

polars is the harder comparison and the more honest one. It's a Rust columnar engine with a multi-threaded CSV reader, it's what people actually move to when pandas gets slow, and it is fast. If csvql's design has a real advantage, polars is where it has to show up.

It does, but not mainly in the place we expected.

## The numbers

Same file, same query, all three in-process through their Python APIs. No subprocess, no CLI, so this measures engines and not process startup, which is the measurement that makes csvql look best and is therefore the one to exclude.

2,000,000 rows, 72.6 MB:

```
  library       seconds   vs csvql    peak RSS   vs csvql
  csvql        0.013619      1.00x     75.5 MB      1.00x
  pandas       0.542779     39.86x    433.3 MB      5.74x
  polars       0.031163      2.29x    299.6 MB      3.97x
```

20,000,000 rows, 745.6 MB:

```
  library       seconds   vs csvql    peak RSS   vs csvql
  csvql        0.111548      1.00x     75.9 MB      1.00x
  pandas       6.720655     60.25x   1258.6 MB     16.59x
  polars       0.361476      3.24x   1296.0 MB     17.09x
```

2.29x and 3.24x on time. Worth having, not worth a blog post on its own. The column that matters is the one on the right.

## csvql's memory does not move. polars' does, then stops

Look at csvql's peak RSS across those two tables. The file got 10.3x bigger. The memory went from 75.5 MB to 75.9 MB.

The obvious story to tell here is that polars scales with the file and csvql doesn't. We wrote that, then measured it at two more sizes, and it's wrong. Peak RSS on the same `GROUP BY`, one run per library in its own child process:

| file | csvql | polars | polars as multiple of file |
|---|---|---|---|
| 73 MB | 74.1 MB | 302.1 MB | 4.16x |
| 746 MB | 75.0 MB | 1518.6 MB | 2.04x |
| 2032 MB | 75.4 MB | 1844.8 MB | 0.91x |
| 4044 MB | 75.4 MB | 2054.3 MB | 0.51x |

polars grows sub-linearly and flattens out. From 2 GB to 4 GB the file doubled and polars added 210 MB. Extrapolating the early ratio would have predicted around 17 GB for a 10 GB file, which is not what this curve is heading toward at all.

The reason is a real polars strength: it isn't holding the CSV, it's holding parsed typed columns. Three of this fixture's six columns are low-cardinality strings, and dictionary-encoded columns are far smaller than their text. A 4 GB CSV genuinely does not need 4 GB of columns. Any claim that a columnar engine's memory tracks file size is a claim about text, not about what the engine actually allocates.

So the honest comparison is between two flat lines at different heights. csvql settles at about 75 MB and polars at about 2 GB, which is 27x at the 4 GB mark. csvql's line is flat because it never builds a dataset at all: it memory-maps the file, scans it with SIMD, and keeps only the aggregate state the query asks for. For `GROUP BY department` that state is a handful of departments, a few kilobytes whether the file holds two million rows or a hundred million. The 75 MB is mostly mapped pages the OS is counting, not structures csvql allocated.

One measurement caveat: polars' peak RSS moved between runs at the same size, 1296 MB in the earlier table and 1519 MB here on the 746 MB file. These are single runs, not medians, because peak RSS needs a fresh process. csvql's did not vary meaningfully. Treat the polars figures as approximate and the shape of the curve as the finding.

## Where it matters and where it doesn't

At 4 GB, polars needs about 2 GB and csvql about 75 MB. On a 16 GB laptop running one query, that difference is invisible, and this is not an argument for switching. The 2 GB plateau is modest and polars is a mature library with a far larger surface than csvql's SQL subset.

It starts mattering where a 2 GB floor stops being free:

- Several queries at once multiply the floor. 75 MB does not.
- Containers with a hard memory limit kill the process rather than slowing down, and a 2 GB baseline sets how small that limit can be.
- An agent querying files it did not pick can't predict the peak in advance, which is the case csvql was built for.

What we are no longer claiming, having measured it: that polars falls over on large files. It doesn't.

## Three query shapes, so this isn't one lucky benchmark

A single `GROUP BY` is cherry-picking. Two more shapes on the same files, including sorting, which is polars' strongest ground and csvql's weakest because `ORDER BY` has to materialize rows:

| query shape | 72.6 MB | 745.6 MB |
|---|---|---|
| `GROUP BY department` | 2.29x | 3.24x |
| `WHERE age > 40 AND city = 'Boston'` + agg | 2.10x | 2.62x |
| `ORDER BY salary DESC LIMIT 10` | 1.48x | 1.60x |

Multiples of polars' time, so higher is better for csvql. The lead is narrowest exactly where theory says it should be: top-N forces csvql to hold rows, which is the one place its memory model gives nothing away and polars' columnar sort is genuinely good. 1.48x is the floor, not the headline.

The lead also grows with file size on all three shapes, which is the opposite of what we found against DuckDB, where [the ratio shrank as files got bigger](journey-to-10x-duckdb-csv.html) once process startup stopped dominating. There's no startup to amortize here: everything is in-process. What's left is that polars' per-row cost of building columns keeps growing while csvql's per-row cost of scanning bytes doesn't.

## Reproducing it

```sh
./bench/gen_fixture.sh fixture.csv 2000000
./bench/bench_libs.py fixture.csv 9
```

`gen_fixture.sh` has no randomness and no timestamps, so a given row count always produces the same bytes. `bench_libs.py` reports the median of N runs after a warm-up run, and measures peak RSS in a separate single-run child per library, because RSS is a high-water mark and three libraries in one process would all report whichever made the largest allocation.

The four-size RSS table is the same thing at 54,000,000 and 107,000,000 rows as well, running only the peak-RSS child per library since that is all it reports.

Apple M2 Pro, 12 cores, 16 GB, macOS 26.5.1. csvql 2.7.0, polars 1.44.1, pandas 3.0.3, Python 3.14.4. The two extra query shapes and the four-size RSS sweep aren't in the committed script; they're the same harness with the query swapped and the timing dropped. We did not test a 10 GB file: polars would be in swap on 16 GB of RAM, which measures the swap subsystem rather than the engine.

## What we are claiming

csvql is 1.5 to 3.2x faster than polars on these queries and holds peak memory at about 75 MB from a 73 MB file to a 4 GB one. polars is fast and its memory plateaus around 2 GB rather than tracking the file, which is better than we assumed before measuring it.

We are not claiming csvql replaces polars. It reads one CSV at a time and answers a subset of SQL; polars is a dataframe library. The claim is narrower: the memory is flat and low enough that you don't have to think about it, and you don't have to know how big the file is before you query it.
