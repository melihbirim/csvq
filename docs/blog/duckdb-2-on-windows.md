---
layout: post
title: "DuckDB 2.0 Says It's 6x Faster on Windows. I Checked, and Found My Own Engine Was the Slow One."
description: "DuckDB reported TPC-H SF300 going from 822 s to 129 s on Windows. On raw-CSV queries the v2.0 alpha measured 11-18% faster than 1.5.5. The same benchmark showed csvql barely ahead on Windows, because it was copying the whole file into memory before reading a byte. Fixing that took the Windows lead from 1.3x to 6x."
date: 2026-09-29
---

*DuckDB says v2.0 is more than 6x faster on Windows. On raw CSV I measured 11–18%. The more useful finding was that my own engine had been quietly slow on Windows the whole time.*

---

## The claim

On 28 September DuckDB reported TPC-H at scale factor 300 finishing in **129 seconds on v2.0-dev versus 822 seconds on v1.5.6**, on Windows. That's more than 6x.

It's a vendor number. Versions, hardware and the hot-run method were stated; per-query results and reproduction scripts were not. It is also not a raw-CSV benchmark: TPC-H runs joins and aggregations over data already loaded into DuckDB's own format.

[csvql](https://github.com/melihbirim/csvql) only does one thing: query raw CSV in place. So the question for me wasn't "is the 6x real?" but "does DuckDB 2.0 close the gap on the work csvql actually does?" The only way to answer that is to measure it.

---

## How I measured it

Same rules as the [earlier NYC taxi comparison](csvql-vs-duckdb-nyc-taxi.html): DuckDB's own taxi dataset, the four canonical Billion-Taxi-Rides queries, and both engines reading the **raw CSV directly**, with no preload into a native store.

What's new is where it runs. I don't own a Windows machine, so it runs on GitHub Actions: a [manually triggered workflow](https://github.com/melihbirim/csvql/blob/main/.github/workflows/bench-duckdb-versions.yml) that builds csvql and downloads DuckDB 1.5.5 and the current v2.0 alpha, all on the same VM.

Shared CI runners are noisy, so the harness ([`bench/bench_taxi_versions.py`](https://github.com/melihbirim/csvql/blob/main/bench/bench_taxi_versions.py)) is built around that:

- **Answers first, timing second.** Every engine's result is checked against csvql's before anything is timed. A query whose answers differ gets no speed number at all. Every result in this post matched.
- **Engines alternate.** Each round runs csvql, DuckDB 1.5.5 and DuckDB 2.0 in turn, rotating the order every round, so a VM that slows down halfway through penalizes all three equally.
- **Median of 5 rounds**, after one warm-up pass so the OS page cache is equally warm for everyone.
- **Ratios, not seconds.** A 4-vCPU cloud VM says nothing about your laptop. What it can say is how the engines compare *on the same machine*.
- **Three VMs per run.** If the ratio swings a lot between VMs, the result is noise. The spread is reported next to every number.

The alpha that actually ran was `v2.0.0-alpha43650`, a slightly newer build than the `alpha43586` DuckDB tested. The download link is a rolling "latest alpha" link, so I recorded the version each run reported.

---

## First result: DuckDB 2.0 nearly caught up

1M-row sample (425 MB), `windows-latest`, csvql's lead (DuckDB time ÷ csvql time), range across three VMs:

| Query | vs DuckDB 1.5.5 | vs DuckDB 2.0 alpha |
|---|---|---|
| Q01 `COUNT(*) GROUP BY cab_type` | 1.52–1.73x | 1.22–1.39x |
| Q02 `AVG(total_amount) GROUP BY passenger_count` | 1.51–1.68x | 1.23–1.39x |
| Q03 `GROUP BY passenger_count, year` | 1.32–1.45x | 1.07–1.17x |
| Q04 `GROUP BY passenger_count, year, ROUND(distance)` | 1.31–1.39x | 1.07–1.15x |

Against 2.0, Q03 and Q04 are close to a draw. The easy headline would have been "DuckDB 2.0 catches csvql on Windows."

But look at the left column. **Against the old DuckDB 1.5.5, csvql was only 1.3–1.7x ahead on Windows.** On macOS, the same sample shows about a 10x lead. Nothing about DuckDB 2.0 explains that. Something was wrong on my side.

---

## The bug: Windows never memory-mapped anything

csvql's scanners are built around `mmap`: map the file, split it into line-aligned chunks, and let every core fault its own pages in straight from the page cache. On POSIX, that's what happened. On Windows, the helper that maps the file had this:

```zig
if (builtin.os.tag == .windows) {
    return file.readToEndAlloc(allocator, @intCast(size));
}
```

A single-threaded copy of the **entire file** into a heap buffer before the first byte is scanned. The parallel scanners then ran on the copy, but they all had to wait for that copy first. It also meant an 8 GB CSV needed 8 GB of RAM just to start.

It had been like this since Windows support landed at the end of June. No test caught it, because the answers were always correct; it was only slow. And no benchmark caught it, because every benchmark I had published ran on macOS or Linux.

The fix maps a read-only view with `CreateFileMappingW` + `MapViewOfFile`, the Windows equivalent of `mmap`. The same helper had been copied into three files, so it now lives in one place ([`src/file_map.zig`](https://github.com/melihbirim/csvql/blob/main/src/file_map.zig)). [PR #194](https://github.com/melihbirim/csvql/pull/194).

---

## After the fix

Same workflow, same sample, same three-VM setup:

| Query | vs 1.5.5, before | vs 1.5.5, **after** | vs 2.0 alpha, before | vs 2.0 alpha, **after** |
|---|---|---|---|---|
| Q01 | 1.52–1.73x | **6.10–6.40x** | 1.22–1.39x | **4.95–5.28x** |
| Q02 | 1.51–1.68x | **5.51–6.06x** | 1.23–1.39x | **4.51–4.91x** |
| Q03 | 1.32–1.45x | **3.28–3.59x** | 1.07–1.17x | **2.65–2.99x** |
| Q04 | 1.31–1.39x | **3.11–3.23x** | 1.07–1.15x | **2.57–2.71x** |

csvql's Q01 time on Windows went from about 0.5 s to 0.14 s. The VM-to-VM spread was 4–12%, so the change is far larger than the noise.

---

## The full 8 GB file

The sample flatters csvql, because on small files DuckDB's startup cost is a large share of the total. The real test is the full `trips_xaa` file: 20M rows, about 8 GB.

**Linux** (`ubuntu-latest`), csvql's lead, range across three VMs:

| Query | vs DuckDB 1.5.5 | vs DuckDB 2.0 alpha |
|---|---|---|
| Q01 | 3.71–3.74x | 3.19–3.21x |
| Q02 | 3.40–3.43x | 2.96–3.00x |
| Q03 | 1.95–1.96x | 1.70x |
| Q04 | 1.82–1.83x | 1.57–1.58x |

The spread between VMs was 0–1%: the Linux runners are much steadier than the Windows ones.

**Windows** (`windows-latest`): the 8 GB run was still in progress when this post went up. The table will be added here once it finishes.

---

## So how much faster is DuckDB 2.0?

On these raw-CSV queries, measured on the same machines, the v2.0 alpha is faster than 1.5.5 by:

- **~18% on Windows** (1M-row sample, Q01)
- **~11% on Linux** (1M-row sample, Q01)
- **~14% on Linux** (8 GB file, Q01: 8.09 s → 6.95 s)

That's a real improvement, and it narrows csvql's lead. It is nowhere near 6x, and it isn't supposed to be: the 6x claim is about TPC-H on loaded data, a different workload. The two numbers don't contradict each other. They measure different things.

---

## What this doesn't show

- **These are shared cloud VMs**, 4 vCPUs, not DuckDB's hardware. The ratios are meaningful; the absolute seconds aren't comparable with anything else.
- **It's an alpha.** I'll rerun on DuckDB 2.0 final when it ships in October and update this post.
- **It's four queries on one dataset**: aggregations over raw CSV. It says nothing about joins, loaded tables, or TPC-H.
- **I didn't reproduce DuckDB's TPC-H number**, and this post doesn't claim it's wrong.

---

## The real lesson

I set out to check a competitor's claim. The benchmark's most useful output was a bug in my own code. It had shipped for three months because I only ever benchmarked on the two platforms I own.

A benchmark that runs where your users are, checks every answer, and reports its own noise is worth more than any headline number, including mine. The workflow is in the repo and runs on demand: point it at any DuckDB build URL and pick an OS.
