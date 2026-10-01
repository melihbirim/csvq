---
layout: post
title: "The Journey to 10x DuckDB on CSV Parsing and Querying"
description: "csvql reads unquoted CSV up to 12.7x faster than DuckDB on one thread. Add quotes and the lead used to vanish entirely. A running log of finding out why, the fixes that did not work, and the prefix XOR that did."
date: 2026-10-01
---

*csvql reads unquoted CSV 5 to 10x faster than DuckDB on a single thread. Put quotes around one field and the entire lead disappears. This post is the running log of chasing that down: what the measurements say, which theories died, and what gets tried next. It will be updated as the work lands or fails.*

---

## The anomaly

A routine benchmark, 2.8 GB of support tickets, one filter:

```
SELECT COUNT(*) FROM 'tickets.csv' WHERE region = 'apac'

  csvql   1.22s
  duckdb  1.05s
```

DuckDB won. That was surprising enough to stop and check, because on other files csvql wins comfortably. The one unusual thing about this file is a long free-text column, and free text gets quoted.

So: build two files that differ in exactly one way.

```python
# same 4,000,000 rows, same columns, same words
f.write(f'{i},{region},{text}\n')      # q_off.csv   200,881,338 bytes
f.write(f'{i},{region},"{text}"\n')    # q_on.csv    208,881,338 bytes
```

Same row count. Four percent more bytes. Here is the result, single thread:

| | csvql | DuckDB |
|---|---|---|
| unquoted | **3.94 GB/s** | 0.73 GB/s |
| quoted | 0.97 GB/s | 0.95 GB/s |

csvql is 5.4x faster than DuckDB until a quote appears, and then it is exactly level. Four percent more bytes cost four times the throughput.

---

## DuckDB does not care about quotes at all

The obvious next question is what DuckDB is doing differently. Sweeping the quoted field width, one thread, same query:

| field width | csvql rows/s | csvql GB/s | DuckDB rows/s | DuckDB GB/s |
|---|---|---|---|---|
| 16 B | 30.9 M | 0.99 | 11.0 M | **0.35** |
| 256 B | 16.1 M | 4.38 | 2.7 M | **0.74** |
| 1024 B | 5.5 M | 5.69 | 0.5 M | **0.53** |

My first guess was that DuckDB is row-bound: that its cost sits in per-row work
like type casting and vector materialisation, so the bytes in between barely
register. That is testable, and it is wrong. A row-bound engine would hold
roughly constant rows per second as fields grow. **DuckDB's rows/s collapses
from 11.0 M to 0.5 M**, a factor of twenty-two across a sixty-four-fold change
in field width. It is paying for those bytes.

So read the GB/s columns instead.

csvql gets **faster per byte as fields get longer**, 0.99 to 5.69 GB/s. DuckDB
stays **flat: 0.35, 0.74, 0.53**, no matter what the data looks like. Sixty-four
times more bytes per row changes its per-byte rate by less than a factor of two,
with no trend.

Not row-bound, and a near-constant cost per byte that is indifferent to what the
byte contains. That is the signature of a per-byte state machine: load a byte,
classify it, transition the DFA, repeat. A quote costs exactly what a letter costs, which is exactly what a comma costs. Nothing is ever skipped, so quoting cannot take anything
away. There is nothing to lose.

csvql works the opposite way. It loads 16 bytes at a time, computes masks for the delimiter, the newline and the quote, and if none of them hit, it skips the entire chunk:

```zig
const combined: u16 = nl_mask | q_mask | delim_mask;
if (combined == 0) continue;   // 16 bytes, no work
```

Long fields mean long runs of boring bytes, and csvql jumps over them. That is the whole 5 to 10x.

**So quoting does not make csvql slow. It makes csvql ordinary.** A quote lands in `q_mask`, the skip stops firing, and csvql falls back to touching every byte, which is what DuckDB does on every byte of every file. The two engines converging at 0.97 and 0.95 is not a coincidence. It is csvql being reduced to DuckDB's strategy.

That reframes the target. The goal is not to beat DuckDB on quoted data, because we already match it. The goal is for quoted data to skip the way unquoted data does.

---

## Three fixes that did not work

Worth recording, because each one sounded right.

**Replace the byte loop with SIMD jumps.** The quoted fallback was a byte-at-a-time state machine. It became a loop of `indexOfScalarPos` jumps between quotes and newlines, which is a real improvement in isolation. The key realisation was that `at_field_start`, which looks like state that must be carried, is derivable: outside a quote a delimiter sets it, a newline returns, everything else clears it, so it is true only at the record start or right after a delimiter. On the 2.8 GB file: 1.10s to 0.87s.

**Make the fused SIMD scan quote-aware.** `scanRecordFused` already found the record end, the field positions and any quote in one vector pass, and then returned the instant it saw a quote and let the caller start over. Carrying `in_quote` through the loop, so a chunk containing no quote inside a quoted field is skipped whole, took the file to 0.97s against DuckDB's 1.24s.

**Stop parsing every record twice.** Even with a quote-aware scan, the caller branched on `had_quote` and called `findRecordEnd` and `parseCSVFieldsStatic` to rediscover what the one pass had already established. A quoted row was scanned three times to learn what one pass knew. Removing that: `SELECT UPPER(region)` went 1.02s to 0.81s.

All three were measured, all three were verified byte-identical against adversarial quoting, 103 correctness checks and 700 fuzzer queries. All three were then reverted.

They were reverted because they treat symptoms. Together they got the quoted path from roughly 2.1 to 2.9 GB/s on twelve threads, which is a real gain and still nowhere near the 4 to 5.7 GB/s the unquoted path reaches. They make the fallback cheaper. They do not restore the skipping, and the skipping is the entire advantage.

Two theories died along the way, both worth not repeating:

- **It is a parallelism problem.** It is not. Quoted and unquoted scale about the same, 8.3x against 10.8x at twelve threads. An earlier reading that suggested otherwise came from a fixture too small to measure, which is its own lesson.
- **It is the per-field escape search.** Every quoted field was scanned end to end to ask whether it contained a `""`, a question the scan had already answered. Genuinely redundant, genuinely O(bytes). Removing it produced inconsistent results and a 12% regression on the large fixture, so it did not ship.

---

## Update, 1 October: prefix XOR works, and we are not at 10x

The fix has to restore skipping while quotes are present, which means knowing
whether a byte is inside a quoted field without branching on each quote. That is
a prefix XOR over the quote mask:

```
quote_mask  = chunk == '"'
in_quote    = prefix_xor(quote_mask) ^ carry_in
structural  = (delim_mask | nl_mask) & ~in_quote
```

Six shift-and-xor pairs on a 64-bit mask. No branch per quote, no restart, and
`""` escapes cancel for free because two toggles are no toggle.

Measured on the scan alone, quoted input, 200 MB at each width:

| field width | scalar | prefix XOR | speedup |
|---|---|---|---|
| 16 B | 1.39 GB/s | 2.13 GB/s | 1.53x |
| 64 B | 1.87 GB/s | 3.67 GB/s | 1.97x |
| 256 B | 1.94 GB/s | 7.20 GB/s | 3.70x |
| 1024 B | 2.11 GB/s | **9.75 GB/s** | 4.61x |

The scalar column is flat. The prefix XOR column climbs with field width. That is
the skipping coming back, which was the whole point.

### The correctness problem, and how it was caught

Prefix XOR toggles on every quote. csvql only opens a field on a quote at a field
start, so the two disagree whenever a stray quote sits inside an unquoted field.
The block now checks that every field the XOR view claims to open really is at a
field start, and declines to the scalar parser otherwise.

Getting that guard right took two attempts, and a differential test against the
existing implementation found both:

- The first version treated any run of adjacent quotes as escape pairs. A run of
  three, `bq"""`, is an odd number, so one of them really does toggle. The test
  shrank it to a 64-byte case within seconds.
- The second version was correct but declined **100%** of records in any file
  containing a `""` escape, which would have made the whole thing useless on real
  data. The second quote of an escape pair reads as a fresh open to the XOR view,
  so a preceding quote has to count as a valid opener too.

Final behaviour, measured per record on realistic files:

| file shape | fast path |
|---|---|
| plain unquoted | 99% |
| every field quoted | 99% |
| one quoted text column | 99% |
| quoted with embedded comma | 99% |
| quoted with embedded newline | 99% |
| quoted with `""` escape | 99% |
| stray mid-field quotes | 0%, correctly declines |

300,000 random quote-heavy inputs produced zero disagreements with the scalar
implementation.

### So are we at 10x on quoted data

No. One thread, `COUNT(*)` with a `WHERE`:

| field width | csvql quoted | DuckDB quoted | ratio | csvql unquoted | DuckDB unquoted | ratio |
|---|---|---|---|---|---|---|
| 16 B | 0.94 GB/s | 0.38 GB/s | 2.5x | 0.96 GB/s | 0.35 GB/s | 2.7x |
| 256 B | 1.53 GB/s | 0.97 GB/s | 1.6x | 3.94 GB/s | 0.73 GB/s | 5.4x |
| 1024 B | 1.90 GB/s | 0.67 GB/s | 2.9x | 6.64 GB/s | 0.52 GB/s | **12.7x** |

The 10x is real, and it is on unquoted data. On quoted data this moved csvql from
dead level with DuckDB to roughly twice its speed, which is progress and is not
the target.

The remaining gap is against ourselves, not DuckDB: 1.53 GB/s quoted against 3.94
GB/s unquoted at 256-byte fields. Record-end scanning is now fast. Field splitting
still makes its own quote-aware pass over every quoted row, so the record is read
twice: once to find where it ends, once to find where its fields are. The fused
scan already computes both. Making the caller use what it computed is the next
step, and it is worth roughly another 2x if the earlier experiments are any guide.

## Caveats

- Every number here is single thread unless stated, measured on an M2 Pro against DuckDB 1.5.5, best of two or three runs, with the fixtures in page cache. They measure parse speed, not disk.
- The claim that DuckDB uses a per-byte state machine is **inferred from its throughput curve**, not read from its source. A flat GB/s across a 64x change in field width is strong evidence for a scanner that never skips, but it is evidence, not a citation.
- The three attempts in the previous section were reverted. The prefix XOR work is on a branch and not yet merged.
- The honest state of the comparison today: csvql is 5 to 10x faster than DuckDB on unquoted CSV on one thread, and level on quoted. The README's older "2.8x" figure comes from a specific benchmark file and should not be read as a general claim in either direction.

Next update will cover the field-splitting pass, which is where the remaining 2.6x against our own unquoted path lives.
