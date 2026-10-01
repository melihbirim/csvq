#!/usr/bin/env bash
# bench_quoted_vs_duckdb.sh -- csvql against DuckDB on quoted and unquoted CSV,
# single threaded, across WHERE selectivity and projection.
#
# The point of the fixture pair is that it isolates quoting: same rows, same
# columns, same text, and the only difference is whether `note` is wrapped in
# quotes. An earlier version of this benchmark used a filter that matched every
# row, which measured nothing, so the regions here are 50% apac, 49% emea and
# 1% rare.
#
# Generate the fixtures first (see docs/blog/journey-to-10x-duckdb-csv.md), then:
#   ./bench/bench_quoted_vs_duckdb.sh /path/to/fixture/dir
set -uo pipefail
D="$1"; C=/Users/melihbirim/code/birim.one.dev/csvql/zig-out/bin/csvql
m(){ best=99; for i in 1 2 3; do s=$( { /usr/bin/time -p "$@" >/dev/null; } 2>&1 | awk '/^real/{print $2}'); awk -v a="$s" -v b="$best" 'BEGIN{exit !(a<b)}' && best=$s; done; echo "$best"; }
row(){ lbl="$1"; f="$2"; where="$3"; sel="$4"
  sz=$(stat -f%z "$D/$f.csv")
  cq="SELECT $sel FROM '$D/$f.csv'${where:+ WHERE $where}"
  dq="SELECT $sel FROM read_csv('$D/$f.csv',header=true)${where:+ WHERE $where}"
  a=$(m $C --threads 1 "$cq"); b=$(m duckdb -csv -c "SET threads=1; $dq")
  printf "  %-34s %6.2f  %6.2f   %5.1fx\n" "$lbl" \
    "$(echo "$sz/1e9/$a"|bc -l)" "$(echo "$sz/1e9/$b"|bc -l)" "$(echo "$b/$a"|bc -l)"
}
echo "                                       csvql  duckdb   ratio"
echo "UNQUOTED (371 MB, 1.43M rows, 256B note)"
row "COUNT(*), no WHERE"          sel_u ""                "COUNT(*)"
row "COUNT(*) WHERE 50% match"    sel_u "region='apac'"   "COUNT(*)"
row "COUNT(*) WHERE 1% match"     sel_u "region='rare'"   "COUNT(*)"
row "SELECT note WHERE 1% match"  sel_u "region='rare'"   "note"
echo "QUOTED (373 MB, same rows)"
row "COUNT(*), no WHERE"          sel_q ""                "COUNT(*)"
row "COUNT(*) WHERE 50% match"    sel_q "region='apac'"   "COUNT(*)"
row "COUNT(*) WHERE 1% match"     sel_q "region='rare'"   "COUNT(*)"
row "SELECT note WHERE 1% match"  sel_q "region='rare'"   "note"
