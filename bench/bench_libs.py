#!/usr/bin/env python3
"""csvql's Python binding against pandas and polars, same query, same file.

In-process on all three: no subprocess, no CLI, so this measures the engines
rather than process startup. Reports the median of N runs and peak RSS from a
separate single-run child, because RSS is a high-water mark and repeated runs
in one process would report the largest allocation any of them made.

    ./bench/bench_libs.py <csv> [runs]
"""
import os, statistics, subprocess, sys, time

CSV = sys.argv[1] if len(sys.argv) > 1 else "large_test.csv"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 9
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

Q = f"SELECT department, COUNT(*), AVG(salary) FROM '{CSV}' GROUP BY department"


def bench(fn):
    fn()  # warm the page cache and any lazy import
    return statistics.median(
        (lambda s=time.perf_counter(): (fn(), time.perf_counter() - s)[1])() for _ in range(N)
    )


def rss_of(lib):
    """Peak RSS of a child doing one run, in bytes."""
    out = subprocess.run(
        [sys.executable, __file__, CSV, "1", "--rss", lib],
        capture_output=True, text=True,
    )
    return int(out.stdout.strip())


def run_csvql():
    import csvql
    return csvql.query(Q)


def run_pandas():
    import pandas as pd
    return pd.read_csv(CSV).groupby("department").agg(n=("salary", "size"), avg=("salary", "mean"))


def run_polars():
    import polars as pl
    return pl.read_csv(CSV).group_by("department").agg(pl.len(), pl.col("salary").mean())


LIBS = {"csvql": run_csvql, "pandas": run_pandas, "polars": run_polars}

if "--rss" in sys.argv:
    import resource
    LIBS[sys.argv[sys.argv.index("--rss") + 1]]()
    # macOS reports ru_maxrss in bytes, Linux in kilobytes.
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(m if sys.platform == "darwin" else m * 1024)
    sys.exit()

size = os.path.getsize(CSV)
print(f"{CSV}  {size/1e6:.1f} MB  median of {N} runs")
print(f"  {'library':<9}{'seconds':>12}{'vs csvql':>11}{'peak RSS':>12}{'vs csvql':>11}")
times, rss = {}, {}
for name, fn in LIBS.items():
    times[name] = bench(fn)
    rss[name] = rss_of(name)
for name in LIBS:
    print(f"  {name:<9}{times[name]:>12.6f}{times[name]/times['csvql']:>10.2f}x"
          f"{rss[name]/1e6:>9.1f} MB{rss[name]/rss['csvql']:>10.2f}x")
