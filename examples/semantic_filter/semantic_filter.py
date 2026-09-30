#!/usr/bin/env python3
"""Answer a question that is part SQL and part judgment, over a CSV too large to send anywhere.

    "Which enterprise billing tickets from the last quarter are from angry
     customers at risk of churning?"

"enterprise", "billing" and "last quarter" are exact predicates: csvql answers
them over the raw CSV, locally, in one scan. "angry" and "at risk of churning"
are judgments: Jev answers those, for the handful of rows that survived.

The ordering is the whole point. Judging 20 million rows would cost real money
and take hours; judging the 1,700 that matter costs a fraction of a cent. The
scan is also the only stage that touches the full file, so everything except the
surviving rows stays on this machine.

Usage:
    export TYPESAFE_API_KEY=...
    ./semantic_filter.py tickets.csv
    ./semantic_filter.py tickets.csv --dry-run    # stage 1 only, no API key needed
    ./semantic_filter.py tickets.csv --limit 200  # cap rows sent to the model

Requires: csvql on PATH (or CSVQL=/path/to/csvql). No Python dependencies.
"""

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Stage 1. Everything here is an exact predicate, so it belongs in SQL, not in a
# model. Narrow the columns too: the model never needs ticket_id or region, and
# every column carried into stage 2 is tokens paid for on every surviving row.
PREFILTER_SQL = """
SELECT customer_id, plan, category, created_at, replies, csat, body
FROM '{csv}'
WHERE plan = 'enterprise'
  AND category = 'billing'
  AND created_at >= '2026-07-01'
  AND replies >= 4
  AND csat <= 2
"""

# Stage 2. Three independent judgments about the same ticket. The docs are
# explicit that independent questions over one state should go in a single
# request: they run in parallel, and the state is sent once instead of three
# times. Sending these as three calls would roughly triple the input tokens.
QUESTIONS = {
    "angry": {
        "type": "noul",
        "instructions": (
            "Is the customer in `ticket.body` expressing anger or frustration "
            "at the company, as opposed to merely reporting a problem?"
        ),
        "criteria": {
            "true": "Frustration is directed at the company or its handling: repeated unanswered "
                    "contact, sarcasm, complaints about being ignored, or explicit anger.",
            "false": "Neutral, polite, or apologetic in tone, even if the underlying problem is "
                     "serious or the customer is disappointed.",
        },
    },
    "churn_risk": {
        "type": "noul",
        "instructions": (
            "Does `ticket.body` indicate the customer is considering leaving, cancelling, "
            "or moving to a competitor?"
        ),
        "criteria": {
            "true": "States or strongly implies they may cancel, not renew, or switch vendors.",
            "false": "No indication of leaving, even if annoyed.",
        },
    },
    "next_action": {
        "type": "choice",
        "instructions": "What should happen to this ticket next?",
        "criteria": {
            "escalate_to_human": "Needs an account manager or senior support contact today.",
            "reply_normally": "Can be handled in the normal support queue.",
            "no_action": "Already resolved or needs nothing further.",
        },
    },
}


def run_prefilter(csv_path, csvql_bin):
    """Stage 1: csvql reduces the file. Returns (rows, seconds, bytes_scanned)."""
    sql = PREFILTER_SQL.format(csv=csv_path).strip()
    started = time.perf_counter()
    proc = subprocess.run(
        [csvql_bin, sql], capture_output=True, text=True, check=False
    )
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        sys.exit(f"csvql failed: {proc.stderr.strip() or proc.stdout.strip()}")
    rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    return rows, elapsed, os.path.getsize(csv_path)


def judge(row, api_key):
    """Stage 2: one request per ticket, three questions inside it."""
    payload = {
        # Named fields, not one blob: the questions reference `ticket.body`, and
        # the surrounding fields are the context that makes "angry" judgeable.
        "state": {
            "ticket": {
                "body": row["body"],
                "plan": row["plan"],
                "category": row["category"],
                "replies_so_far": row["replies"],
                "last_csat_score": row["csat"],
            }
        },
        "model": MODEL,
        "questions": QUESTIONS,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        return {"row": row, "error": f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"}
    except Exception as e:  # noqa: BLE001 - surfaced per row, not swallowed
        return {"row": row, "error": str(e)}

    answers = body.get("answers", {})
    return {
        "row": row,
        "angry": answers.get("angry", {}).get("noul"),
        "churn_risk": answers.get("churn_risk", {}).get("noul"),
        "next_action": answers.get("next_action", {}).get("choice"),
        "action_confidence": answers.get("next_action", {}).get("confidence"),
        "usage": body.get("usage", {}),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    ap.add_argument("--dry-run", action="store_true", help="stage 1 only, no API key needed")
    ap.add_argument("--limit", type=int, default=0, help="cap rows sent to the model (0 = all)")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="report rows where both angry and churn_risk exceed this")
    args = ap.parse_args()

    csvql_bin = os.environ.get("CSVQL", "csvql")

    rows, elapsed, total_bytes = run_prefilter(args.csv_path, csvql_bin)
    kept_bytes = sum(len(r.get("body", "")) for r in rows)
    print(f"stage 1  csvql scan   {total_bytes / 1e9:8.2f} GB -> {len(rows):,} rows "
          f"in {elapsed:.2f}s, locally")
    if total_bytes:
        print(f"         data leaving this machine: {kept_bytes / 1e6:.2f} MB "
              f"({100 * kept_bytes / total_bytes:.4f}% of the file)")

    if args.limit:
        rows = rows[: args.limit]
    if args.dry_run:
        print(f"stage 2  skipped (--dry-run); would judge {len(rows):,} rows")
        return

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit("TYPESAFE_API_KEY is not set (use --dry-run to see stage 1 alone)")

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda r: judge(r, api_key), rows))
    judged_elapsed = time.perf_counter() - started

    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    input_tokens = sum(r.get("usage", {}).get("input_tokens", 0) for r in ok)

    print(f"stage 2  Jev judged   {len(ok):,} rows in {judged_elapsed:.2f}s "
          f"({len(errors)} errors), {input_tokens:,} input tokens")

    # Policy lives in code, not in the model. The judgments are reusable
    # probabilities; this threshold is a product decision and changing it does
    # not require re-judging anything.
    flagged = [
        r for r in ok
        if (r["angry"] or 0) >= args.threshold and (r["churn_risk"] or 0) >= args.threshold
    ]
    flagged.sort(key=lambda r: -(r["churn_risk"] or 0))

    print(f"\n{len(flagged)} ticket(s) angry AND churn-risk above {args.threshold}:\n")
    for r in flagged[:20]:
        row = r["row"]
        print(f"  {row['customer_id']}  angry={r['angry']:.2f}  churn={r['churn_risk']:.2f}  "
              f"-> {r['next_action']}")
        print(f"    {row['body'][:96]}")
    if errors:
        print(f"\nfirst error: {errors[0]['error']}")


if __name__ == "__main__":
    main()
