---
layout: post
title: "csvql vs polars: The Memory Stays Flat"
description: "polars is what people move to when pandas gets slow, and it is genuinely fast. On the same queries csvql is 1.5 to 3.2x ahead of it, and uses 17x less memory on a 746 MB file, because it never loads the file."
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

## csvql's memory does not move

Look at csvql's peak RSS across those two tables. The file got 10.3x bigger. The memory went from 75.5 MB to 75.9 MB.

polars went from 299.6 MB to 1296 MB. pandas from 433.3 MB to 1258.6 MB. Both scale with the file, because both are doing the same fundamental thing: read the CSV, build an in-memory representation of all of it, then run the query against that representation. polars' representation is far more efficient than pandas' and its reader is much faster, which is exactly why it is 17 to 19x quicker than pandas here. But it's still proportional to the input.

csvql's isn't, because csvql never builds that representation. It memory-maps the file, scans it with SIMD, and accumulates only the aggregate state the query asks for. For `GROUP BY department` that state is a handful of departments, so it's a few kilobytes regardless of whether the file has two million rows or twenty million. The 76 MB is almost entirely the mapped pages the OS is counting, not data structures csvql allocated.

That's the structural difference, and it's the one thing in this comparison a tuning pass can't close. polars could get twice as fast at reading CSV and the memory line would be identical.

## Where it matters and where it doesn't

On a 16 GB laptop, 1.3 GB for a 746 MB file is fine. Nobody notices. The difference starts mattering at the point where "proportional to the file" stops fitting:

- A 10 GB CSV needs roughly 17 GB in polars by this ratio. That's a machine you have to go buy.
- Several queries running at once multiply the peak. Flat memory doesn't.
- Containers with a hard memory limit kill the process rather than slowing down. There's no graceful degradation to tune.
- An agent querying files it didn't pick can't predict the peak in advance, which is the case csvql was built for.

If your files comfortably fit in RAM and always will, this is not an argument for switching. polars is a mature, well-documented library with a much larger surface than csvql's SQL subset, and for dataframe work in a notebook it is the better tool. The flat-memory property only buys you something when the file size is out of your control.

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

Apple M2 Pro, 12 cores, 16 GB, macOS 26.5.1. csvql 2.7.0, polars 1.44.1, pandas 3.0.3, Python 3.14.4. The two extra query shapes aren't in the committed script; they're the same harness with the query swapped.

## What we are claiming

csvql is 1.5 to 3.2x faster than polars on these queries and holds peak memory at roughly the size of the input regardless of file size. We are not claiming csvql replaces polars. It reads one CSV at a time and answers a subset of SQL; polars is a dataframe library. The claim is narrower and it's the one we care about: if you need to query a CSV whose size you don't control, the memory is flat.
