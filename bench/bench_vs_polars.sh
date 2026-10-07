#!/usr/bin/env bash
# bench_vs_polars.sh — csvql against polars and DuckDB on the shapes where the
# answer actually differs between engines.
#
# Every engine runs in its own process, one query per process, so nothing is
# cached between calls and no engine's warm-up subsidises another's. Engines are
# interleaved round by round rather than run A-then-B, because this machine's
# load drifts enough over a few minutes to invent a 40% difference on its own.
# The first round of each shape is discarded: it pays for pulling the fixture
# into page cache, which is a disk measurement, not an engine one.
#
# Reports the median of the remaining rounds. polars is driven through
# scan_csv (lazy), which is its recommended path for a file on disk and the
# faster of the two on every shape here.
#
# Usage:
#   ./bench/bench_vs_polars.sh <csv> [rounds]
#
# Needs: ./zig-out/bin/csvql built ReleaseFast, python3 with polars and duckdb.

set -uo pipefail

CSV="${1:?usage: bench_vs_polars.sh <csv> [rounds]}"
ROUNDS="${2:-6}"
PY="${PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for mod in polars duckdb; do
  "$PY" -c "import $mod" 2>/dev/null || { echo "need $mod in $PY"; exit 1; }
done

# One query, one process, one timing. Keeping this in a single file rather than
# three keeps the three engines' timing harness provably identical.
cat > "$WORK/run.py" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[4])
CSV, ENGINE, SHAPE, _root = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

if ENGINE == "csvql":
    import csvql
    Q = {
        "count":      f"SELECT COUNT(*) FROM '{CSV}'",
        "sum":        f"SELECT SUM(salary) FROM '{CSV}'",
        "groupby":    f"SELECT department, COUNT(*), AVG(salary) FROM '{CSV}' GROUP BY department",
        "where":      f"SELECT COUNT(*) FROM '{CSV}' WHERE city = 'Boston'",
        "topn":       f"SELECT name, salary FROM '{CSV}' ORDER BY salary DESC LIMIT 10",
        "limit":      f"SELECT name, salary FROM '{CSV}' LIMIT 10",
    }[SHAPE]
    run = lambda: csvql.query(Q)
elif ENGINE == "polars":
    import polars as pl
    run = {
        "count":   lambda: pl.scan_csv(CSV).select(pl.len()).collect(),
        "sum":     lambda: pl.scan_csv(CSV).select(pl.col("salary").sum()).collect(),
        "groupby": lambda: pl.scan_csv(CSV).group_by("department").agg(pl.len(), pl.col("salary").mean()).collect(),
        "where":   lambda: pl.scan_csv(CSV).filter(pl.col("city") == "Boston").select(pl.len()).collect(),
        "topn":    lambda: pl.scan_csv(CSV).sort("salary", descending=True).head(10).select("name", "salary").collect(),
        "limit":   lambda: pl.scan_csv(CSV).select("name", "salary").head(10).collect(),
    }[SHAPE]
else:
    import duckdb
    Q = {
        "count":   f"SELECT COUNT(*) FROM read_csv('{CSV}')",
        "sum":     f"SELECT SUM(salary) FROM read_csv('{CSV}')",
        "groupby": f"SELECT department, COUNT(*), AVG(salary) FROM read_csv('{CSV}') GROUP BY department",
        "where":   f"SELECT COUNT(*) FROM read_csv('{CSV}') WHERE city = 'Boston'",
        "topn":    f"SELECT name, salary FROM read_csv('{CSV}') ORDER BY salary DESC LIMIT 10",
        "limit":   f"SELECT name, salary FROM read_csv('{CSV}') LIMIT 10",
    }[SHAPE]
    run = lambda: duckdb.sql(Q).fetchall()

s = time.perf_counter()
run()
print(f"{time.perf_counter() - s:.6f}")
PY

SHAPES="count sum groupby where topn limit"
SIZE_MB=$(awk -v b="$(wc -c < "$CSV")" 'BEGIN{printf "%.1f", b/1048576}')
echo "$CSV  ${SIZE_MB} MB  median of $((ROUNDS - 1)) rounds (first discarded)"
printf "  %-9s %11s %11s %11s %9s %9s\n" shape csvql polars duckdb "vs pl" "vs dd"

median() { sort -n | awk '{v[NR]=$1} END{print (NR%2) ? v[(NR+1)/2] : (v[NR/2]+v[NR/2+1])/2}'; }

for shape in $SHAPES; do
  : > "$WORK/cq" ; : > "$WORK/pl" ; : > "$WORK/dd"
  for r in $(seq 1 "$ROUNDS"); do
    c=$("$PY" "$WORK/run.py" "$CSV" csvql  "$shape" "$ROOT/python")
    p=$("$PY" "$WORK/run.py" "$CSV" polars "$shape" "$ROOT/python")
    d=$("$PY" "$WORK/run.py" "$CSV" duckdb "$shape" "$ROOT/python")
    [ "$r" -eq 1 ] && continue   # discard the page-cache round
    echo "$c" >> "$WORK/cq" ; echo "$p" >> "$WORK/pl" ; echo "$d" >> "$WORK/dd"
  done
  mc=$(median < "$WORK/cq") ; mp=$(median < "$WORK/pl") ; md=$(median < "$WORK/dd")
  awk -v s="$shape" -v c="$mc" -v p="$mp" -v d="$md" \
    'BEGIN{printf "  %-9s %11.6f %11.6f %11.6f %8.2fx %8.2fx\n", s, c, p, d, p/c, d/c}'
done
