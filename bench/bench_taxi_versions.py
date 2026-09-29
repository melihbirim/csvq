#!/usr/bin/env python3
"""
bench_taxi_versions.py — csvql vs several DuckDB builds on the canonical NYC
Taxi raw-CSV queries (same queries as bench_taxi.sh), portable to Windows.

Built to answer one question: does a new DuckDB build narrow csvql's lead on
the same machine? Shared CI runners are noisy and differ run to run, so:

  * engines are interleaved inside every round (and the order rotates each
    round), so a VM slowdown hits every engine alike;
  * the reported number is the MEDIAN over rounds, not the mean;
  * the headline is the ratio engine/csvql on the same VM, not seconds;
  * the answer of every engine is checked against csvql before any timing is
    reported; a mismatched query gets no speed number.

Usage:
  bench_taxi_versions.py fetch  --out DIR [--full]
  bench_taxi_versions.py run    --csv FILE --csvql EXE --engine NAME=EXE ...
                                [--rounds 5] [--json out.json]
  bench_taxi_versions.py aggregate  results/*.json

Results are also appended as markdown to $GITHUB_STEP_SUMMARY when set.
"""

import argparse
import gzip
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

BLOB = "https://blobs.duckdb.org/data/nyc-taxi-dataset"
SAMPLE_ROWS = 1_000_000

# 51-column header (taxi-benchmark schema.sql) — the source files are headerless.
HEADER = (
    "trip_id,vendor_id,pickup_datetime,dropoff_datetime,store_and_fwd_flag,rate_code_id,"
    "pickup_longitude,pickup_latitude,dropoff_longitude,dropoff_latitude,passenger_count,"
    "trip_distance,fare_amount,extra,mta_tax,tip_amount,tolls_amount,ehail_fee,"
    "improvement_surcharge,total_amount,payment_type,trip_type,pickup,dropoff,cab_type,"
    "precipitation,snow_depth,snowfall,max_temperature,min_temperature,average_wind_speed,"
    "pickup_nyct2010_gid,pickup_ctlabel,pickup_borocode,pickup_boroname,pickup_ct2010,"
    "pickup_boroct2010,pickup_cdeligibil,pickup_ntacode,pickup_ntaname,pickup_puma,"
    "dropoff_nyct2010_gid,dropoff_ctlabel,dropoff_borocode,dropoff_boroname,dropoff_ct2010,"
    "dropoff_boroct2010,dropoff_cdeligibil,dropoff_ntacode,dropoff_ntaname,dropoff_puma"
)


def queries(csv):
    """Canonical Billion-Taxi-Rides queries; kept in sync with bench_taxi.sh.
    csvql uses STRFTIME where DuckDB uses DATE_PART."""
    dk = f"read_csv_auto('{csv}')"
    return [
        ("Q01",
         f"SELECT cab_type, COUNT(*) FROM '{csv}' GROUP BY cab_type",
         f"SELECT cab_type, COUNT(*) FROM {dk} GROUP BY cab_type"),
        ("Q02",
         f"SELECT passenger_count, AVG(total_amount) FROM '{csv}' GROUP BY passenger_count",
         f"SELECT passenger_count, AVG(total_amount) FROM {dk} GROUP BY passenger_count"),
        ("Q03",
         f"SELECT passenger_count, STRFTIME('%Y',pickup_datetime) AS y, COUNT(*) FROM '{csv}' "
         f"GROUP BY passenger_count, y",
         f"SELECT passenger_count, DATE_PART('year',pickup_datetime) AS y, COUNT(*) FROM {dk} "
         f"GROUP BY passenger_count, y"),
        ("Q04",
         f"SELECT passenger_count, STRFTIME('%Y',pickup_datetime) AS y, ROUND(trip_distance) AS d, "
         f"COUNT(*) AS c FROM '{csv}' GROUP BY passenger_count, y, d ORDER BY y, c DESC",
         f"SELECT passenger_count, DATE_PART('year',pickup_datetime) AS y, ROUND(trip_distance) AS d, "
         f"COUNT(*) AS c FROM {dk} GROUP BY passenger_count, y, d ORDER BY y, c DESC"),
    ]


# ── fetch ────────────────────────────────────────────────────────────────────

def cmd_fetch(a):
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "trips.csv" if a.full else "sample.csv")
    if os.path.exists(path):
        print(f"cached: {path}")
        return
    limit = None if a.full else SAMPLE_ROWS
    print(f"fetching trips_xaa ({'full ~8 GB' if a.full else f'{limit} rows'}) -> {path}", flush=True)
    tmp = path + ".part"
    # Stream-decompress straight to disk: the .gz never lands, so the full file
    # needs ~8 GB free, not ~10.
    req = urllib.request.Request(f"{BLOB}/trips_xaa.csv.gz", headers={"User-Agent": "Mozilla/5.0 (csvql-bench)"})
    with urllib.request.urlopen(req) as resp, \
            gzip.GzipFile(fileobj=resp) as gz, open(tmp, "wb") as out:
        out.write(HEADER.encode() + b"\n")
        if limit is None:
            shutil.copyfileobj(gz, out, 1 << 22)
        else:
            for n, line in enumerate(gz):
                if n >= limit:
                    break
                out.write(line)
    os.replace(tmp, path)
    print(f"done: {os.path.getsize(path) / 1e6:.0f} MB")


# ── run ──────────────────────────────────────────────────────────────────────

def argv_for(kind, exe, cq, dq):
    return [exe, cq] if kind == "csvql" else [exe, "-csv", "-c", dq]


def version_of(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001 — report, don't crash the benchmark
        return f"unknown ({e})"


def norm_cell(s):
    """Numbers compare by value (csvql prints '2012' where DuckDB prints 2012;
    AVG may differ in the last printed digit), everything else as text."""
    s = s.strip().strip('"')
    try:
        f = float(s)
    except ValueError:
        return s
    return f"{f:.9g}"


def norm_rows(text):
    """Data rows only (header naming is cosmetic: COUNT(*) vs count_star()),
    CRLF-safe, sorted (GROUP BY order is undefined; Q04 ties can reorder)."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    return sorted(",".join(norm_cell(c) for c in ln.split(",")) for ln in lines[1:])


def free_mb():
    """Available physical memory in MB, or None where we can't tell cheaply."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullAvailPhys // (1 << 20)
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001 — diagnostics only
        pass
    return None


def timed(argv, stdout_mode):
    """Wall time of one run, or None if it failed (stderr tail is printed).
    stdout_mode "pipe" reads and discards output; "devnull" sends it to the
    null device, which on Windows some CLIs treat as an interactive console."""
    out = subprocess.PIPE if stdout_mode == "pipe" else subprocess.DEVNULL
    t = time.perf_counter()
    p = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE)
    dt = time.perf_counter() - t
    if p.returncode != 0:
        tail = p.stderr.decode(errors="replace").strip()[-500:]
        print(f"    FAILED exit {p.returncode} after {dt:.1f}s: {argv[0]}\n    stderr: {tail!r}", flush=True)
        return None
    return dt


def md_table(res):
    engines = [e for e in res["engines"] if e != "csvql"]
    lines = [
        f"### csvql vs DuckDB — {res['rows']:,} rows, {res['csv_mb']:.0f} MB, "
        f"median of {res['rounds']} interleaved rounds",
        "",
        f"`{res['host']}` · {res['cpus']} logical CPUs · " +
        " · ".join(f"**{n}**: `{v}`" for n, v in res["versions"].items()),
        "",
        "| Query | csvql | " + " | ".join(f"{e} | {e}/csvql" for e in engines) + " |",
        "|---|---:|" + "---:|---:|" * len(engines),
    ]
    for q, r in res["queries"].items():
        c = r["median"]["csvql"]
        row = [q, "FAILED" if c is None else f"{c:.3f}s"]
        for e in engines:
            if not r["match"].get(e, False):
                row += ["MISMATCH", "—"]
            elif e not in r["ratio"]:
                row += ["FAILED", "—"]
            else:
                row += [f"{r['median'][e]:.3f}s", f"**{r['ratio'][e]:.2f}x**"]
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "Ratio > 1 means csvql is faster. A MISMATCH or FAILED cell reports no speed."]
    return "\n".join(lines)


def cmd_run(a):
    csv = os.path.abspath(a.csv).replace("\\", "/")  # forward slashes: both engines accept them on Windows
    engines = {"csvql": ("csvql", a.csvql)}
    for spec in a.engine:
        name, exe = spec.split("=", 1)
        engines[name] = ("duckdb", exe)
    names = list(engines)
    qs = queries(csv)

    res = {
        "host": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpus": os.cpu_count(),
        "rows": sum(1 for _ in open(csv, "rb")) - 1,
        "csv_mb": os.path.getsize(csv) / 1e6,
        "rounds": a.rounds,
        "stdout_mode": a.stdout,
        "engines": names,
        "versions": {n: version_of(exe) for n, (_, exe) in engines.items()},
        "queries": {},
    }
    print(json.dumps({k: res[k] for k in ("host", "cpus", "rows", "versions")}, indent=2), flush=True)

    # 1) Correctness first — every engine's answer vs csvql's, before any timing.
    for q, cq, dq in qs:
        outs = {}
        for n, (kind, exe) in engines.items():
            p = subprocess.run(argv_for(kind, exe, cq, dq), capture_output=True, text=True)
            outs[n] = norm_rows(p.stdout) if p.returncode == 0 else None
            if p.returncode != 0:
                print(f"{q} {n}: exit {p.returncode}: {p.stderr.strip()[:300]}", flush=True)
        match = {n: outs[n] is not None and outs[n] == outs["csvql"] for n in names if n != "csvql"}
        for n, ok in match.items():
            if not ok and outs[n] is not None and outs["csvql"] is not None:
                extra = sorted(set(outs[n]) ^ set(outs["csvql"]))[:10]
                print(f"{q} {n}: MISMATCH vs csvql, first differing rows: {extra}", flush=True)
        res["queries"][q] = {"match": match, "times": {n: [] for n in names}}
        print(f"{q} correctness: {match}", flush=True)

    # 2) Timing — one warm-up pass (page cache equal for everyone), then
    #    interleaved rounds with the engine order rotated every round. Every
    #    run is logged with free memory, so a slowdown can be pinned on one
    #    engine, one query, or the VM as a whole.
    def run_logged(label, q, n, argv):
        dt = timed(argv, a.stdout)
        shown = "FAILED" if dt is None else f"{dt:7.2f}s"
        print(f"  {label:>7} {q} {n:<18} {shown}  free={free_mb()} MB", flush=True)
        return dt

    print(f"stdout mode: {a.stdout}", flush=True)
    for q, cq, dq in qs:
        for n, (kind, exe) in engines.items():
            run_logged("warm-up", q, n, argv_for(kind, exe, cq, dq))
    for r in range(a.rounds):
        order = names[r % len(names):] + names[:r % len(names)]
        for q, cq, dq in qs:
            for n in order:
                kind, exe = engines[n]
                res["queries"][q]["times"][n].append(run_logged(f"round {r + 1}", q, n, argv_for(kind, exe, cq, dq)))
        print(f"round {r + 1}/{a.rounds} done", flush=True)

    failed = False
    for q, r in res["queries"].items():
        ok = {n: [t for t in ts if t is not None] for n, ts in r["times"].items()}
        failed |= any(len(v) < len(r["times"][n]) for n, v in ok.items())
        r["median"] = {n: statistics.median(v) if v else None for n, v in ok.items()}
        r["ratio"] = {n: r["median"][n] / r["median"]["csvql"]
                      for n in names if n != "csvql" and r["median"][n] and r["median"]["csvql"]}

    table = md_table(res)
    print("\n" + table)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=2)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(table + "\n\n")
    if failed or not all(all(r["match"].values()) for r in res["queries"].values()):
        sys.exit(1)


# ── aggregate ────────────────────────────────────────────────────────────────

def cmd_aggregate(a):
    runs = [json.load(open(p)) for p in a.files if os.path.exists(p)]
    if not runs:
        sys.exit("no result files: every bench job failed before producing one (see their logs)")
    engines = [e for e in runs[0]["engines"] if e != "csvql"]
    lines = [
        f"### Ratio stability across {len(runs)} VMs (engine/csvql, median per VM)",
        "",
        "Tight spread = real signal. Wide spread = the runner is too noisy to conclude.",
        "",
        "| Query | " + " | ".join(f"{e} per VM | {e} spread" for e in engines) + " |",
        "|---|" + "---|---:|" * len(engines),
    ]
    for q in runs[0]["queries"]:
        row = [q]
        for e in engines:
            if not all(r["queries"][q]["match"].get(e, False) for r in runs):
                row += ["MISMATCH on ≥1 VM", "—"]
                continue
            if not all(e in r["queries"][q].get("ratio", {}) for r in runs):
                row += ["FAILED on ≥1 VM", "—"]
                continue
            vals = [r["queries"][q]["ratio"][e] for r in runs]
            spread = (max(vals) - min(vals)) / statistics.median(vals) * 100
            row += [" / ".join(f"{v:.2f}x" for v in vals), f"{spread:.0f}%"]
        lines.append("| " + " | ".join(row) + " |")
    table = "\n".join(lines)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(table + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    f.add_argument("--full", action="store_true", help="whole trips_xaa (~8 GB) instead of the 1M-row sample")
    r = sub.add_parser("run")
    r.add_argument("--csv", required=True)
    r.add_argument("--csvql", required=True)
    r.add_argument("--engine", action="append", required=True, help="NAME=PATH to a DuckDB CLI; repeatable")
    r.add_argument("--rounds", type=int, default=5)
    r.add_argument("--stdout", choices=["pipe", "devnull"], default="pipe",
                   help="where timed runs send stdout (default: pipe, as in the correctness pass)")
    r.add_argument("--json")
    g = sub.add_parser("aggregate")
    g.add_argument("files", nargs="+")
    a = p.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run, "aggregate": cmd_aggregate}[a.cmd](a)


if __name__ == "__main__":
    main()
