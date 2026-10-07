---
layout: post
title: "csvql vs polars: The Memory Stays Flat"
description: "polars is what people move to when pandas gets slow, and it is genuinely fast. On the same queries csvql is 1.5 to 3.2x ahead of it, at a flat 75 MB of memory from a 73 MB file to a 4 GB one, against polars settling around 2 GB."
date: 2026-10-05
---

*Updated 2026-10-07: two numbers in this post were wrong. The original text is unchanged; see [the update at the end](#update-2026-10-07-polars-20-and-two-numbers-above-that-were-wrong).*

Beating pandas is not an interesting claim. pandas loads the whole CSV into a DataFrame before it will answer anything, so any engine that doesn't do that wins by a margin that says more about the comparison than the engine. We [wrote that post](csvql-vs-pandas.html) and the number was 17 to 76x.

polars is the harder comparison and the more honest one. It's a Rust columnar engine with a multi-threaded CSV reader, it's what people actually move to when pandas gets slow, and it is fast. If csvql's design has a real advantage, polars is where it has to show up.

It does, though in memory rather than mainly in speed.

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

## Two flat lines at different heights

Look at csvql's peak RSS across those two tables. The file got 10.3x bigger. The memory went from 75.5 MB to 75.9 MB.

Two sizes aren't enough to describe how either library scales, so here are four. Peak RSS on the same `GROUP BY`, one run per library in its own child process:

| file | csvql | polars | polars as multiple of file |
|---|---|---|---|
| 73 MB | 74.1 MB | 302.1 MB | 4.16x |
| 746 MB | 75.0 MB | 1518.6 MB | 2.04x |
| 2032 MB | 75.4 MB | 1844.8 MB | 0.91x |
| 4044 MB | 75.4 MB | 2054.3 MB | 0.51x |

Neither line tracks file size. csvql's is flat outright. polars' grows sub-linearly and flattens: from 2 GB to 4 GB the file doubled and polars added 210 MB. Read only the first two rows and the multiples look like a proportional curve, which is exactly the trap in a two-point measurement.

The reason is a real polars strength: it isn't holding the CSV, it's holding parsed typed columns. Three of this fixture's six columns are low-cardinality strings, and dictionary-encoded columns are far smaller than their text. A 4 GB CSV genuinely does not need 4 GB of columns. Any claim that a columnar engine's memory tracks file size is a claim about text, not about what the engine actually allocates.

So the comparison is between two flat lines at different heights. csvql settles at about 75 MB and polars at about 2 GB, which is 27x at the 4 GB mark. csvql's line is flat because it never builds a dataset at all: it memory-maps the file, scans it with SIMD, and keeps only the aggregate state the query asks for. For `GROUP BY department` that state is a handful of departments, a few kilobytes whether the file holds two million rows or a hundred million. The 75 MB is mostly mapped pages the OS is counting, not structures csvql allocated.

One measurement caveat: polars' peak RSS moved between runs at the same size, 1296 MB in the earlier table and 1519 MB here on the 746 MB file. These are single runs, not medians, because peak RSS needs a fresh process. csvql's did not vary meaningfully. Treat the polars figures as approximate and the shape of the curve as the finding.

## Where it matters and where it doesn't

At 4 GB, polars needs about 2 GB and csvql about 75 MB. On a 16 GB laptop running one query, that difference is invisible, and this is not an argument for switching. The 2 GB plateau is modest and polars is a mature library with a far larger surface than csvql's SQL subset.

It starts mattering where a 2 GB floor stops being free:

- Several queries at once multiply the floor. 75 MB does not.
- Containers with a hard memory limit kill the process rather than slowing down, and a 2 GB baseline sets how small that limit can be.
- An agent querying files it did not pick can't predict the peak in advance, which is the case csvql was built for.

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

csvql is 1.5 to 3.2x faster than polars on these queries and holds peak memory at about 75 MB from a 73 MB file to a 4 GB one. polars is fast and its memory plateaus around 2 GB rather than tracking the file.

We are not claiming csvql replaces polars. It reads one CSV at a time and answers a subset of SQL; polars is a dataframe library. The claim is narrower: the memory is flat and low enough that you don't have to think about it, and you don't have to know how big the file is before you query it.

---

## Update, 2026-10-07: polars 2.0, and two numbers above that were wrong

Everything above this line is the post as first published, left as written. This section is what changed and what we got wrong, because a correction that quietly edits the original teaches nobody anything.

Three things happened after publication. polars 2.0 shipped. We found that the benchmark above used polars' eager API when its lazy one is both recommended and faster. And chasing the gap that exposed turned up a correctness bug of our own.

### The measurement above used the wrong polars API

`pl.read_csv()` loads the file, then queries it. `pl.scan_csv()` builds a lazy plan and lets polars push work into the scan. For a file on disk the second is what polars tells you to use, and on these queries it is the faster of the two. The post above used the first one throughout.

That mattered most on exactly the shape the post called "polars' strongest ground":

```
ORDER BY salary DESC LIMIT 10, 745.6 MB
  polars 1.44.1 eager   1.504s
  polars 1.44.1 lazy    1.094s
  polars 2.0.0  eager   2.119s
  polars 2.0.0  lazy    0.290s
```

polars 2.0's streaming engine is 3.8x faster than 1.44.1 on the lazy path and slower on the eager one, so which API you call now changes the answer by 7x. We had picked the slow one and reported the result as a 1.60x win.

### So the top-N claim above is wrong

The table above says `ORDER BY salary DESC LIMIT 10` was 1.48x and 1.60x in csvql's favour. Against `scan_csv` we were losing it, by about 6x at 20M rows. Not by a little, and not because of polars 2.0: polars 1.44.1's lazy path already beat us, 1.094s against our 1.794s, on the day we published.

The cause was ours. `ORDER BY x LIMIT 10` had every worker append every row it saw, the merge copied all of them into sort keys, and only then did the top-K heap keep ten and discard twenty million. The right algorithm ran after the cost had already been paid. Each worker now keeps its own bounded heap of K, so the merge sorts threads*K entries instead of the whole file.

That took the query from 2.06s to 0.38s. It did not produce a win:

```
20M rows / 711 MB, ORDER BY salary DESC LIMIT 10
  csvql   0.344s
  polars  0.305s
  duckdb  0.420s
```

We are level with polars and 1.2x ahead of DuckDB. Closing a 6x deficit to a draw is the honest description.

### And the flat-memory claim needs a boundary

The post above says csvql "holds peak memory at about 75 MB from a 73 MB file to a 4 GB one". That is true of aggregates and false of sorts, which the post should have checked rather than reasoned about. On the same 711 MB file:

```
GROUP BY department              csvql    54 MB
ORDER BY salary DESC LIMIT 10    csvql   715 MB
GROUP BY department              polars 1110 MB
```

The flat line is real, and it is a property of aggregate queries, where csvql keeps only the running accumulator. `ORDER BY` has to hold rows, so it tracks the file. 715 MB is down from 1687 MB before the fix above, and what remains is memory-mapped file pages, which the kernel can evict, rather than the ~970 MB of anonymous allocation that used to sit beside them. But it is not 75 MB, and the original sentence should not have implied it was.

### The full picture, including where we lose

One query per process, so nothing is cached between calls, engines interleaved round by round, first round discarded because it pays for page cache. polars 2.0.0 through `scan_csv`. Median of five.

20M rows, 711 MB:

| shape | csvql | polars 2.0 | duckdb | vs polars | vs duckdb |
|---|---|---|---|---|---|
| `COUNT(*)` | 0.0256 | 0.0220 | 0.4005 | **0.86x** | 15.6x |
| `SUM(salary)` | 0.0807 | 0.2268 | 0.3716 | 2.81x | 4.6x |
| `GROUP BY department` | 0.1241 | 0.3173 | 0.4978 | 2.56x | 4.0x |
| `WHERE city = 'Boston'` | 0.0804 | 0.2818 | 0.4351 | 3.51x | 5.4x |
| `ORDER BY salary LIMIT 10` | 0.3440 | 0.3047 | 0.4197 | **0.89x** | 1.2x |
| `LIMIT 10`, no sort | 0.0011 | 0.0029 | 0.0662 | 2.65x | 60.9x |

2M rows, 69.2 MB:

| shape | csvql | polars 2.0 | duckdb | vs polars | vs duckdb |
|---|---|---|---|---|---|
| `COUNT(*)` | 0.0039 | 0.0045 | 0.1064 | 1.15x | 27.1x |
| `SUM(salary)` | 0.0125 | 0.0361 | 0.1132 | 2.88x | 9.0x |
| `GROUP BY department` | 0.0140 | 0.0447 | 0.1159 | 3.19x | 8.3x |
| `WHERE city = 'Boston'` | 0.0094 | 0.0331 | 0.1122 | 3.53x | 12.0x |
| `ORDER BY salary LIMIT 10` | 0.0381 | 0.0412 | 0.1119 | 1.08x | 2.9x |
| `LIMIT 10`, no sort | 0.0011 | 0.0031 | 0.0681 | 2.86x | 61.8x |

The bold cells are losses. `COUNT(*)` at 711 MB is a loss we cannot do much about: counting records means reading every byte, CSV carries no row count to shortcut to, and at 0.0256s for 711 MB both engines are near 29 GB/s and bandwidth-bound rather than compute-bound. It was 3x worse before we stopped splitting every row into fields to increment a counter that never looked at them.

### Where the gap is actually large

Not on the narrow six-column file both of these tables use. On a wide one, which is what a CSV exported from a warehouse or a spreadsheet usually looks like:

```
757 MB, 60 columns, SUM of one column at position 58
  csvql   0.0563s
  polars  0.2187s    3.9x
  duckdb  0.5767s   10.2x
```

csvql finds the fields it needs and skips the rest of each row. A reader that materialises columns pays for all sixty whether the query mentions them or not. That advantage grows with the columns you are not asking about, and it is the one number here worth leading with.

### Reproducing

```sh
./bench/gen_fixture.sh fixture.csv 20000000
./bench/bench_vs_polars.sh fixture.csv 6
```

csvql 2.8.1 plus the `COUNT(*)` change, polars 2.0.0, duckdb 1.5.6 (Python package; the CLI used for differential testing is 1.5.5), Python 3.14.4, Apple M2 Pro, 12 cores, 16 GB, macOS 26.5.1.

One note on provenance, since it affects the numbers in the original post above. `python/csvql/_loader.py` prefers a bundled `python/csvql/libcsvql.dylib` over the one in `zig-out/lib`. The copy on the machine that produced the first set of figures was five days stale, which we only noticed when the Python binding reported 1.5s for a query the CLI finished in 0.35s. No engine change landed in those five days, so the original numbers are probably sound, but they were not verified against the code they claimed to measure. Every figure in this update was taken with that file freshly copied.
