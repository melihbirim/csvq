---
layout: post
title: "Our Tests Covered One Third of the Engine"
description: "103 differential checks against DuckDB, a nightly fuzzer, and a query that returned rows in the wrong order for fifty releases. The queries that catch it were already in the suite. The fixture was 0.65 MB and the bug starts at 5 MB."
date: 2026-10-08
---

csvql compares itself to DuckDB on every commit. 103 checks, every major query shape, output diffed and any mismatch fails the build. A fuzzer runs nightly against a random seed and files what it finds.

`ORDER BY a, b` returned rows sorted on `a` only, with `b` in arbitrary order, on any file over 5 MB. Silently: no error, correct-looking output, wrong rows. It did that for fifty releases.

The part that took a while to admit is that the suite already contained queries that catch it.

## How a suite can contain the test and still not run it

`engine.zig` picks an execution strategy by input size:

```
under 5 MB ................ executeSequential
over 5 MB ................. mmap_engine.executeMapped
over 10 MB, 2+ threads .... parallel_mmap.executeParallelMapped
```

Three separate implementations of the scan, chosen by a `file_stat.size` comparison. That is a normal shape for a query engine: the small case wants no setup, the large case wants memory mapping and threads.

The fixture generator defaults to 20,000 rows, which is 0.65 MB. Both CI jobs call it with no arguments:

```
ci.yml:156            run: ./bench/gen_fixture.sh
nightly-fuzz.yml:52   run: ./bench/gen_fixture.sh
```

0.65 MB is under 5 MB. So all 103 checks took the sequential path. So did every query the fuzzer has ever generated. Two of the three scan implementations, the two that handle the file sizes anyone actually cares about, had never executed in CI.

The bug lived in both of them. `parser.OrderBy` carries the primary key plus a `secondary: []OrderByKey` slice, and:

```
$ git log --oneline -S "secondary" -- src/mmap_engine.zig src/parallel_mmap.zig
(nothing)
```

Zero commits, ever, in either file. Both resolve a single `order_by_col_idx` and sort on that. The sequential path builds a proper multi-key comparator and sorts correctly, so the engine has always had a correct implementation and two fast paths that silently did not use it.

Bisecting on size makes it plain:

```
0.7 MB   ages 58 58 58   correct    (executeSequential)
3.7 MB   ages 58 58 58   correct    (executeSequential)
5.7 MB   ages 38 26 32   WRONG      (mmap_engine)
69.2 MB  ages 38 26 32   WRONG      (parallel_mmap)
```

`--threads 1` does not help, which is what rules out a concurrency bug and points at the size threshold.

## Coverage counted in checks is not coverage

103 is a reassuring number. It is also the wrong unit. The question is not how many assertions ran, it is which code executed, and a size-dispatched engine makes that invisible: nothing in the test output tells you that two thirds of the scan code never ran. The suite was green and honest about every query it checked. It just checked them all against the same branch.

This is a known enough problem that DuckDB has first-class machinery for it. `force_parallelism` makes a query take the parallel path regardless of size, and `debug_force_external` forces out-of-core execution to exercise the spill code. Same tests, different path, by configuration. They also run SQLsmith for random queries, keep a fuzzer that files its own issues, and assert on `EXPLAIN` output so an optimisation that stops firing gets caught.

We had the differential testing and the fuzzer. We did not have any way to say "run that again, somewhere else".

## What we added

`bench/verify_paths.sh` runs the existing battery once per path, on a fixture sized to land in it, and then does the thing the per-path runs cannot do alone: the same queries over the *same bytes* with `--threads 1` and with the default, which drops a 12 MB file from `parallel_mmap` to `mmap_engine`.

That last check is the valuable one. Agreeing with DuckDB separately is weaker than agreeing with each other, because two paths can be wrong in the same way and still match the oracle on a shape the oracle normalises. Our battery sorts results before diffing so that unstable-sort ties do not cause false failures, which is sensible and also means a row-order bug is exactly the kind it cannot see. So the script also compares six order-sensitive queries path against path, then against DuckDB.

Reverting the fix and re-running shows it working:

```
── sequential path (2.0 MB)                        All 103 checks passed
── mmap path (7.1 MB)                              FAILED  2
── parallel path (12.2 MB)                         FAILED  2
── mmap_engine over the 12 MB fixture (threads 1)  FAILED  2
── row order                                       2 of 6 differ from DuckDB
```

The sequential line is the point of the whole exercise. That is what CI used to see.

## Two things that went wrong while building it

**The first version of that proof was a false all-clear.** Deleting the guard left an unused variable, which Zig rejects, so the build failed and the script happily tested the previous binary and reported everything green. A test harness that cannot tell "passed" from "did not run" is worse than no harness, because it produces evidence. The second attempt checks the build's exit code before trusting the result.

**It was slow enough to be a problem.** Four batteries, 99 seconds, against 15 for the single-path run. Profiling showed it was all battery time, since fixture generation is 1.2 seconds for all three sizes. The batteries are separate processes with their own temp directories that read nothing each other writes, so they now run concurrently: 42 seconds, which is the slowest battery rather than the sum. Nothing in the script measures time, so contending for CPU costs nothing that matters.

Then we re-verified that failure detection survived the change, because dropping a `wait` exit code is an easy way to build a suite that always passes.

## The companion bug, which is funnier

While fixing version numbers for the release that carried this fix, the MCP server turned out to be reporting itself as version 1.0.1 to every client that connected. Forty-seven releases of that.

Four files carry the version. `release.yml` rewrites `nodejs/package.json` and `python/pyproject.toml` from the git tag, but neither Zig source, so those two stayed correct only if whoever cut the release remembered to run a local helper script. `src/main.zig` was usually fine because `csvql --version` is visible. `src/mcp.zig` hardcodes the handshake response, including `serverInfo.version`, and nothing in the codebase reads that field, so nothing ever noticed.

It is the same failure as the main one in a smaller frame: correctness that nothing executes is indistinguishable from correctness that nothing checks. There is now a script asserting all four agree, wired into CI and into the release workflow so a tag pushed without a bump fails the release instead of publishing binaries that misreport themselves.

## What we would do differently

Add a way to force the path. DuckDB selects with a setting; we select by generating three fixtures and paying 42 seconds. A `--force-path` debug flag would let the existing 0.65 MB battery and the whole nightly fuzzer cover all three paths at no extra runtime, which is strictly better than what we built. `verify_paths.sh` would stay as the real-size integration check.

And there is a gap in what we built that is worth naming: the script infers which path a query took from the file size it generated. If a threshold moved, it would quietly test the same path three times and still report success. DuckDB asserts on `EXPLAIN` output for exactly this reason. We have no equivalent, so our new coverage test has the same shape of blind spot as the thing it replaced, just smaller.

The general version, for anyone whose engine picks a strategy at runtime: count the paths, not the assertions. If your code branches on input size, or core count, or a cardinality estimate, then your fixture is a coverage decision and probably nobody wrote it down as one. Ours was a default argument in a shell script.
