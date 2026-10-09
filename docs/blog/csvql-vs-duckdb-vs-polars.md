---
layout: post
title: "csvql vs DuckDB vs polars on Raw CSV"
description: "Six query shapes, two file sizes, three engines, one query per process. csvql wins twelve of twelve against DuckDB and eleven of twelve against polars. The one it loses is more interesting than the eleven it wins."
date: 2026-10-09
---

Everything below queries the same CSV file in place. No ingest step, no Parquet conversion, no database to load into. That is the only comparison csvql is built for, and it is also the one where DuckDB and polars are not at their best, so the numbers should be read as "on raw CSV" and not as a claim about either engine generally.

## Method, because it changes the answer

One query per process. Nothing is cached between calls, no engine gets to warm up on the previous question. Engines interleaved round by round rather than run A then B, because this machine's load drifts enough over a few minutes to invent a 40% difference on its own. First round discarded, since it pays for pulling the fixture into page cache, which measures the disk. Median of the remaining five.

polars runs through `scan_csv`, its lazy API. This matters more than any other methodology choice here: `read_csv` got *slower* in polars 2.0 while `scan_csv` got much faster, and on top-N the difference between them is 7x. We benchmarked the eager one first, published, and had to correct it. If you see a polars CSV benchmark that does not say which API it used, it is not a result.

DuckDB reads the CSV directly via `read_csv`. Its faster path wants data in its own format first, which is a real option and a different workflow: our [Parquet breakeven post](parquet-conversion-breakeven.html) puts it at 20 to 50 queries before conversion pays for itself.

Fixtures come from `bench/gen_fixture.sh`, which has no randomness and no timestamps, so a row count always produces identical bytes.

## 20,000,000 rows, 711 MB

```
  shape           csvql      polars      duckdb     vs pl     vs dd
  count        0.026463    0.022453    0.395705     0.85x    14.95x
  sum          0.079848    0.229953    0.369272     2.88x     4.62x
  groupby      0.115075    0.302640    0.443948     2.63x     3.86x
  where        0.072905    0.267859    0.391860     3.67x     5.37x
  topn         0.120343    0.332107    0.449203     2.76x     3.73x
  limit        0.001027    0.003113    0.068762     3.03x    66.95x
```

## 2,000,000 rows, 69.2 MB

```
  shape           csvql      polars      duckdb     vs pl     vs dd
  count        0.003873    0.005254    0.111235     1.36x    28.72x
  sum          0.010815    0.039929    0.114237     3.69x    10.56x
  groupby      0.015102    0.043803    0.114930     2.90x     7.61x
  where        0.009217    0.037447    0.107159     4.06x    11.63x
  topn         0.014070    0.040822    0.110101     2.90x     7.83x
  limit        0.001134    0.003021    0.065833     2.66x    58.05x
```

Twelve comparisons against each engine. csvql wins all twelve against DuckDB and eleven of twelve against polars.

The DuckDB ratios shrink as the file grows, from 7 to 29x down to 3.7 to 15x, and that direction is the honest one: at 69 MB a visible share of DuckDB's total is process and CSV-reader startup, and by 711 MB the actual parse and aggregate work dominates. Anyone quoting a 29x needs to say how big the file was.

## The loss

`COUNT(*)` at 711 MB: 0.0265 against polars' 0.0225. We lose by 18%, and it is the one number here we chased hardest and could not move.

The counting loop is not the problem. Isolated with pages already resident, it runs at 104 GB/s against 107 GB/s for a minimal newline-only loop, so it is already as fast as a loop that does nothing but count newlines. Of the 26ms, about 7ms is counting and the rest is soft-faulting 45,535 pages into the address space, one per 16 KB page.

Four attempts, all measured, all negative:

- `pread` into per-thread buffers instead of mmap: 1.4 to 1.7x **worse** at every buffer size. The copy costs more than the faults.
- `MADV_WILLNEED`: faults went to zero and system time from 1.11s to 0.04s, and wall time got worse. It converts the faults into a synchronous prefault that costs more than it saves.
- A branchless inner loop: slightly worse.
- Per-query fixed overhead: 0.26ms, so not that either.

Counting records in a CSV means reading every byte, and the format carries no row count to shortcut to. Both engines are near 30 GB/s and bandwidth-bound. polars reads into buffers and never takes the faults; we map and do. That is the whole difference, it is worth 3.6ms, and we do not have a lever on it.

It is the only loss in either table. Counting at the smaller size we win, 1.36x, because the fixed costs that dominate a 69 MB scan are ones we pay less of.

The shape that used to be a loss and no longer is, is top-N. It was about 6x behind polars a week ago, until the sort worker turned out to be copying a 4 KB array per row to hold unescaped field values for a WHERE clause the query did not have.

## Where the gap is widest

Not on a six-column file. On a wide one, which is what a CSV exported from a warehouse or a spreadsheet usually is. 757 MB, 60 columns, `SUM` of one column at position 58:

```
csvql    0.052789
polars   0.211114    4.00x
duckdb   0.564783   10.70x
```

csvql finds the fields a query names and skips the rest of each row. An engine that materialises columns pays for all sixty whether the query mentions them or not. The advantage grows with the columns you are not asking about, which is the opposite of how most CSV benchmarks are shaped: they use narrow fixtures, including both of ours above.

## Memory

Peak RSS on `GROUP BY department`, one process each:

```
file      csvql    duckdb    polars
69.2 MB    73 MB    136 MB    212 MB
711 MB     74 MB    223 MB   1140 MB
```

csvql moves 1 MB across a 10x file size increase, because an aggregate holds a running accumulator rather than a dataset. For `GROUP BY department` that state is a handful of departments, a few kilobytes, whether the file has two million rows or a hundred million. The 74 MB is mostly mapped pages the OS counts, not structures csvql allocated.

Worth saying plainly since our earlier post only compared against polars: **DuckDB is much leaner than polars here**, 223 MB against 1140 MB. If you are choosing between those two on memory, that is the comparison, and polars loses it.

And this is a property of aggregates, not of csvql. `ORDER BY` has to hold rows: the same 711 MB file peaks at 715 MB on a top-N query. We published a flat-memory claim without that caveat and had to correct it.

## What this is not

It is not a claim that csvql is a faster engine than DuckDB or polars. They are both doing more: a full database and a full dataframe library against a tool that reads one CSV and answers a subset of SQL. On their own ground, loading data into their own formats, neither of these numbers would survive.

It is a claim about one specific job: a CSV file you did not create, that you want to ask a question about, without first turning it into something else. On that job, on these twelve measurements, csvql is 3.7 to 67x faster than DuckDB and 1.4 to 4.1x faster than polars, except for counting rows on a large file, where polars is 18% faster and we cannot fix it.

## Reproducing

```sh
./bench/gen_fixture.sh fixture.csv 20000000
./bench/bench_vs_polars.sh fixture.csv 6
```

csvql 2.9.0 at `766c51e`, polars 2.0.0, duckdb 1.5.6, Python 3.14.4, Apple M2 Pro, 12 cores, 16 GB, macOS 26.5.1. The wide-file and memory figures use the same harness with the query swapped; the post says so rather than implying they ship in the script.
