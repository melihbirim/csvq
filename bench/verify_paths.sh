#!/usr/bin/env bash
# verify_paths.sh — Run the correctness battery on every execution path.
#
# engine.zig dispatches on input size and thread count:
#
#   under 5 MB ................ executeSequential
#   over 5 MB ................. mmap_engine.executeMapped
#   over 10 MB, 2+ threads .... parallel_mmap.executeParallelMapped
#
# Both differential suites used to run only on the default fixture from
# gen_fixture.sh, which is 20,000 rows and 0.65 MB, so every query in CI took
# the sequential path and the two mmap paths were never executed at all. That
# is how #223 shipped for roughly sixteen releases: multi-key ORDER BY returned
# rows sorted on the primary key only, on any file over 5 MB, because neither
# mmap path resolved the secondary keys and no test ever crossed the threshold.
#
# This runs verify_correctness.sh once per path, on a fixture sized to land in
# it, and then does the check the per-path runs cannot do on their own: the
# same queries over the *same* bytes with --threads 1 and with the default,
# which is mmap_engine against parallel_mmap on identical input. Agreeing with
# DuckDB separately is weaker than agreeing with each other, because two paths
# can both be wrong in the same way and still match the oracle on a query
# shape the oracle normalises.
#
# Usage:
#   ./bench/verify_paths.sh
#
# Environment overrides:
#   DUCKDB_BIN   — path to duckdb binary (default: duckdb in PATH)
#   KEEP         — set to 1 to keep the generated fixtures for inspection
#
# Exit code: 0 = every path passed and the paths agree, 1 = otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CSVQL="$ROOT/zig-out/bin/csvql"
DUCKDB="${DUCKDB_BIN:-duckdb}"

GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

[[ -x "$CSVQL" ]] || { echo "csvql not found at $CSVQL — run: zig build -Doptimize=ReleaseFast"; exit 1; }
command -v "$DUCKDB" >/dev/null 2>&1 || { echo "duckdb not found (set DUCKDB_BIN)"; exit 1; }

WORK="$(mktemp -d)"
cleanup() { [[ "${KEEP:-0}" == "1" ]] && echo "fixtures kept in $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

# Row counts chosen to land either side of the 5 MB and 10 MB thresholds.
# gen_fixture.sh is deterministic, so these are the same bytes every run.
#   60,000 rows  ~=  2.0 MB   sequential
#  210,000 rows  ~=  7.0 MB   mmap_engine
#  360,000 rows  ~= 12.0 MB   parallel_mmap
declare -a NAMES=("sequential" "mmap" "parallel")
declare -a ROWS=(60000 210000 360000)

echo ""
echo -e "${BOLD}Execution-path coverage${RESET}"
echo ""

FAILED=0

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  rows="${ROWS[$i]}"
  csv="$WORK/path_${name}.csv"
  "$SCRIPT_DIR/gen_fixture.sh" "$csv" "$rows" >/dev/null
  mb=$(awk -v b="$(wc -c < "$csv")" 'BEGIN{printf "%.1f", b/1048576}')

  echo -e "${BOLD}── ${name} path (${mb} MB, ${rows} rows)${RESET}"
  if "$SCRIPT_DIR/verify_correctness.sh" "$csv" > "$WORK/out_${name}.txt" 2>&1; then
    tail -4 "$WORK/out_${name}.txt" | grep -E "Total|Pass|All" || true
  else
    FAILED=1
    echo -e "  ${RED}FAILED on the ${name} path${RESET}"
    # Only the failing checks matter here; the full log is long.
    grep -E "FAIL" "$WORK/out_${name}.txt" | head -20
  fi
  echo ""
done

# ── same bytes, two paths ────────────────────────────────────────
# --threads 1 fails the "2+ threads" condition, so a file over 10 MB takes
# mmap_engine instead of parallel_mmap. Running the battery both ways over one
# fixture compares those two paths directly.
echo -e "${BOLD}── mmap vs parallel on identical input${RESET}"

SHIM="$WORK/csvql_t1"
cat > "$SHIM" <<SH
#!/usr/bin/env bash
exec "$CSVQL" --threads 1 "\$@"
SH
chmod +x "$SHIM"

big="$WORK/path_parallel.csv"
if CSVQL_BIN="$SHIM" "$SCRIPT_DIR/verify_correctness.sh" "$big" > "$WORK/out_threads1.txt" 2>&1; then
  tail -4 "$WORK/out_threads1.txt" | grep -E "Total|Pass|All" || true
else
  FAILED=1
  echo -e "  ${RED}FAILED with --threads 1 on a 12 MB file${RESET}"
  grep -E "FAIL" "$WORK/out_threads1.txt" | head -20
fi

# A query battery that agrees with DuckDB on both paths can still differ
# between them in row order, which matters for ORDER BY and LIMIT. Compare the
# two paths' raw output directly on the shapes where order is the answer.
echo ""
echo -e "${BOLD}── row order, path against path${RESET}"
# @ stands in for the table; bash parameter substitution treats a backslash in
# the pattern as literal, so the placeholder has to be quote-free.
ORDER_QUERIES=(
  "SELECT department, age FROM @ ORDER BY department ASC, age DESC LIMIT 20"
  "SELECT name, salary FROM @ ORDER BY salary DESC LIMIT 10"
  "SELECT name, salary FROM @ ORDER BY salary ASC LIMIT 10"
  "SELECT city, name FROM @ ORDER BY city ASC, name DESC LIMIT 25"
  "SELECT department, salary, age FROM @ ORDER BY department, salary DESC, age ASC LIMIT 30"
  "SELECT name FROM @ WHERE age > 40 ORDER BY name ASC LIMIT 15"
)
order_fail=0
# Inside double quotes bash does not treat \' as an escape, so building the
# quoted path with a backslash inserts a literal one and the engine gets a
# path that does not exist. Hold the quote in a variable instead.
SQ="'"
for q in "${ORDER_QUERIES[@]}"; do
  qq="${q/@/$SQ$big$SQ}"
  dq="${q/@/read_csv($SQ$big$SQ)}"
  if ! "$CSVQL" "$qq" > "$WORK/o_par.csv" 2>"$WORK/o_par.err"; then
    order_fail=1; FAILED=1
    echo -e "  ${RED}csvql FAILED${RESET}  ${q:0:52}  $(head -1 "$WORK/o_par.err")"
    continue
  fi
  "$CSVQL" --threads 1 "$qq" > "$WORK/o_seq.csv" 2>/dev/null
  # DuckDB as the third opinion: a difference between our paths is a bug, and
  # which one is wrong is what this tells us.
  if ! "$DUCKDB" -csv -c "$dq" > "$WORK/o_dd.csv" 2>"$WORK/o_dd.err"; then
    order_fail=1; FAILED=1
    echo -e "  ${RED}duckdb FAILED${RESET}  ${q:0:52}  $(head -1 "$WORK/o_dd.err")"
    continue
  fi
  # An empty result on both sides is two broken queries, not agreement.
  if [[ ! -s "$WORK/o_dd.csv" ]]; then
    order_fail=1; FAILED=1
    echo -e "  ${RED}duckdb returned no rows${RESET}  ${q:0:52}"
    continue
  fi
  if ! diff -q "$WORK/o_par.csv" "$WORK/o_seq.csv" >/dev/null 2>&1; then
    order_fail=1; FAILED=1
    echo -e "  ${RED}PATHS DISAGREE${RESET}  ${q:0:64}"
  elif ! diff -q "$WORK/o_par.csv" "$WORK/o_dd.csv" >/dev/null 2>&1; then
    order_fail=1; FAILED=1
    echo -e "  ${RED}BOTH DIFFER FROM DUCKDB${RESET}  ${q:0:64}"
  fi
done
[[ $order_fail -eq 0 ]] && echo -e "  ${GREEN}all ${#ORDER_QUERIES[@]} order-sensitive queries agree across paths and with DuckDB${RESET}"

echo ""
if [[ $FAILED -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}Every execution path matches DuckDB and the paths agree with each other.${RESET}"
else
  echo -e "${RED}${BOLD}Execution paths are not equivalent — see above.${RESET}"
fi
echo ""
exit $FAILED
