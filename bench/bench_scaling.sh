#!/usr/bin/env bash
# bench_scaling.sh -- csvql against DuckDB across fixture sizes, in shell only.
#
# Why amortised timing: at 2M rows a csvql query finishes in about 0.014s, and
# a shell timer resolves 0.01s. Timing one run reports 0.010s, a 28% understatement
# that inflates the ratio -- which is how bench_all.sh came to report 16.6x for a
# query that actually runs at 8.5x. Here N runs are wrapped in a single
# /usr/bin/time and divided, so the effective resolution is 0.01/N seconds.
#
# Startup is not free either and is reported, since at small sizes it is a real
# share of the total: roughly 0.022s for duckdb and 0.005s for csvql.
#
# Usage:
#   ./bench/bench_scaling.sh <csv> [runs]
set -uo pipefail

CSV="${1:?usage: bench_scaling.sh <csv> [runs]}"
N="${2:-20}"
CSVQL="${CSVQL:-./zig-out/bin/csvql}"
DUCKDB_BIN="${DUCKDB_BIN:-duckdb}"

# Mean seconds per run: time the whole loop, divide by N. awk does the division
# so nothing leaves the shell.
mean() {
  "$@" >/dev/null 2>&1                       # warm the page cache first
  local total
  total=$( { /usr/bin/time -p bash -c '
      n="$1"; shift
      for ((i=0;i<n;i++)); do "$@" >/dev/null 2>&1; done
    ' _ "$N" "$@" ; } 2>&1 | awk '/^real/{print $2}' )
  awk -v t="$total" -v n="$N" 'BEGIN{printf "%.4f", t/n}'
}

printf 'fixture: %s (%s)\n' "$CSV" "$(du -h "$CSV" | cut -f1)"
printf 'runs per measurement: %s\n\n' "$N"
printf '  %-38s %-10s %-10s %s\n' "query" "csvql" "duckdb" "speedup"

bench() {
  local sel="$1" where="${2:-}"
  local cq="SELECT $sel FROM '$CSV'${where:+ WHERE $where}"
  local dq="SELECT $sel FROM read_csv('$CSV',header=true)${where:+ WHERE $where}"
  local a b
  a=$(mean "$CSVQL" "$cq")
  b=$(mean "$DUCKDB_BIN" -csv -c "$dq")
  awk -v l="$sel${where:+ WHERE $where}" -v a="$a" -v b="$b" \
    'BEGIN{printf "  %-38s %-10s %-10s %.1fx\n", substr(l,1,38), a"s", b"s", (a>0)?b/a:0}'
}

bench "COUNT(*)"
bench "COUNT(*)" "age > 30"
bench "SUM(salary), AVG(salary)"
bench "MIN(age), MAX(age)"

printf '\nstartup floor (same method, trivial work):\n'
printf '  %-38s %ss\n' "csvql"  "$(mean "$CSVQL" "SELECT COUNT(*) FROM 'test.csv'")"
printf '  %-38s %ss\n' "duckdb" "$(mean "$DUCKDB_BIN" -csv -c "SELECT 1")"
