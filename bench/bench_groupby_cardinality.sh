#!/usr/bin/env bash
# bench_groupby_cardinality.sh — GROUP BY at low, medium and high cardinality on
# a large generated CSV, single thread versus all cores.
#
# Opt-in: nothing in CI or `zig build test` runs this. The data is generated
# with awk (deterministic, no randomness) into a temporary directory and removed
# afterwards, so no fixture is checked in.
#
# Usage:
#   ./bench/bench_groupby_cardinality.sh [rows]        # default 20000000
#
# Environment:
#   BENCH_RUNS     timed runs per case, the median is reported (default 5)
#   BENCH_THREADS  thread count for the parallel column (default: all cores)
#   CSVQL_BIN      binary to test (default: zig-out/bin/csvql)
#   BENCH_KEEP=1   keep the generated CSVs and print their directory
#
# Cardinalities: 100 keys, 100000 keys, and rows/2 key draws (which, because the
# keys are drawn with replacement, is roughly a quarter of the rows in distinct
# keys). The last one is the shape issue #186 is about: millions of groups.
#
# Requires: csvql built with  zig build -Doptimize=ReleaseFast

set -euo pipefail

ROWS="${1:-20000000}"
RUNS="${BENCH_RUNS:-5}"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CSVQL="${CSVQL_BIN:-${SCRIPT_DIR}/zig-out/bin/csvql}"
THREADS="${BENCH_THREADS:-$( (sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null) || echo 4)}"

if [[ ! -x "$CSVQL" ]]; then
  echo "csvql not found at $CSVQL" >&2
  echo "Build it first:  zig build -Doptimize=ReleaseFast" >&2
  exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/csvql-groupby-bench.XXXXXX")"
cleanup() { if [[ "${BENCH_KEEP:-0}" == "1" ]]; then echo "data kept in $WORK"; else rm -rf "$WORK"; fi; }
trap cleanup EXIT

gen() { # gen <file> <distinct key draws>
  awk -v rows="$ROWS" -v k="$2" 'BEGIN {
    print "id,key,amount"
    for (i = 1; i <= rows; i++) {
      h = (i * 2654435761) % 4294967296
      printf "%d,k%d,%d\n", i, h % k, (i * 7919) % 1000
    }
  }' > "$1"
}

now() { python3 -c 'import time; print(time.time())'; }

median_seconds() { # median_seconds <threads> <sql>
  local t="$1" sql="$2" times=() i s e
  for ((i = 0; i < RUNS; i++)); do
    s=$(now)
    "$CSVQL" --threads "$t" "$sql" > /dev/null
    e=$(now)
    times+=("$(python3 -c "print($e - $s)")")
  done
  printf '%s\n' "${times[@]}" | sort -n | awk '{a[NR]=$1} END {printf "%.2f", a[int((NR+1)/2)]}'
}

echo "rows: $ROWS   runs: $RUNS (median)   parallel threads: $THREADS"
echo "machine: $(uname -sm)"
echo
printf '%-14s %-8s %-40s %10s %10s\n' "cardinality" "keys" "query" "1 thread" "$THREADS threads"

for spec in "low:100" "medium:100000" "high:$((ROWS / 2))"; do
  name="${spec%%:*}"
  keys="${spec##*:}"
  file="$WORK/$name.csv"
  gen "$file" "$keys"
  for q in "COUNT(*)" "COUNT(*), SUM(amount), AVG(amount)"; do
    sql="SELECT key, $q FROM '$file' GROUP BY key"
    printf '%-14s %-8s %-40s %9ss %9ss\n' "$name" "$keys" "$q" \
      "$(median_seconds 1 "$sql")" "$(median_seconds "$THREADS" "$sql")"
  done
done
