#!/usr/bin/env python3
"""Merge sweep_serve.py --chunk answers into one verdict per step, per rule, and score each.

    python3 merge_chunks.py eval_serving_c4
    python3 merge_chunks.py eval_serving --rules any majority

Every chunk is verified against the whole SOP, so most steps come back MISSING in
most chunks. A rule decides the clip's verdict for a step from the chunks that
saw it (PASSED or FAILED):

  any       PASSED if any chunk passed it, else FAILED if any failed it
  strict    FAILED if any chunk failed it, else PASSED if any passed it
  majority  the more common of PASSED and FAILED; a tie is FAILED
  longest   the verdict whose interval is longest

A step no chunk saw is MISSING. Timestamps and frames are moved from chunk time
to clip time. Each rule's outputs go to <dir>/merged-<rule>/ in batch's format,
then evaluate.py scores them. Offline: no API key, no network.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from collections import Counter

RULES = ("any", "strict", "majority", "longest")


def to_clip_time(row: dict, offset: float, fps: float) -> dict:
    row = dict(row)
    for key in ("timestamp_start", "timestamp_end"):
        if row.get(key) is not None:
            row[key] = round(row[key] + offset, 3)
    for key in ("frame_start", "frame_end"):
        if row.get(key) is not None:
            row[key] = row[key] + round(offset * fps)
    return row


def span(row: dict) -> float:
    if row.get("timestamp_start") is None or row.get("timestamp_end") is None:
        return 0.0
    return row["timestamp_end"] - row["timestamp_start"]


def decide(rule: str, seen: list[dict]) -> tuple[str, list[dict]]:
    """The step's status under `rule`, and the rows that decided it."""
    by = {s: [r for r in seen if r["status"] == s] for s in ("PASSED", "FAILED")}
    if not seen:
        return "MISSING", []
    if rule == "any":
        status = "PASSED" if by["PASSED"] else "FAILED"
    elif rule == "strict":
        status = "FAILED" if by["FAILED"] else "PASSED"
    elif rule == "majority":
        status = "PASSED" if len(by["PASSED"]) > len(by["FAILED"]) else "FAILED"
    else:
        best = max(seen, key=span)
        return best["status"], [best]
    return status, by[status]


def merge(doc: dict, rule: str) -> dict:
    rows = [(c["offset"], to_clip_time(r, c["offset"], doc["fps"]))
            for c in doc["chunks"] for r in c.get("rows") or []]
    results = []
    for step in sorted({r["step"] for _, r in rows if r.get("step") is not None}):
        mine = [(o, r) for o, r in rows if r.get("step") == step]
        seen = [r for _, r in mine if r.get("status") in ("PASSED", "FAILED")]
        status, decided = decide(rule, seen)
        tally = Counter(r.get("status") for _, r in mine)
        out = {"step": step, "status": status,
               "chunks": " ".join(f"{s}x{n}" for s, n in sorted(tally.items())),
               "timestamp_start": None, "timestamp_end": None, "frame_start": None, "frame_end": None}
        if decided:
            for key, pick in (("timestamp_start", min), ("timestamp_end", max),
                              ("frame_start", min), ("frame_end", max)):
                vals = [r[key] for r in decided if r.get(key) is not None]
                out[key] = pick(vals) if vals else None
            offsets = {id(r): o for o, r in mine}
            out["reason"] = " | ".join(dict.fromkeys(
                f"[{offsets[id(r)]:.0f} s] {r.get('reason')}" for r in decided))
        else:
            out["reason"] = f"no chunk saw this step ({out['chunks']})"
        results.append(out)
    return {"id": doc["clip"], "results": results}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="a sweep_serve.py --chunk output directory")
    ap.add_argument("--rules", nargs="+", choices=RULES, default=list(RULES))
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.dir, "chunks", "*.json")))
    if not paths:
        sys.exit(f"no chunk answers in {args.dir}/chunks/: run sweep_serve.py --chunk first")
    docs = [json.load(open(p)) for p in paths]
    unanswered = [f"{d['clip']}@{c['offset']}" for d in docs for c in d["chunks"] if c.get("rows") is None]
    if unanswered:
        print(f"WARNING: {len(unanswered)} chunk(s) got no answer, merged without them: {', '.join(unanswered)}")

    for rule in args.rules:
        out_dir = os.path.join(args.dir, f"merged-{rule}")
        os.makedirs(out_dir, exist_ok=True)
        for doc in docs:
            with open(os.path.join(out_dir, f"tva-output-{doc['clip']}.json"), "w") as f:
                f.write(json.dumps(merge(doc, rule)) + "\n")
        report = subprocess.run([sys.executable, "evaluate.py", *sorted(glob.glob(f"{out_dir}/tva-output-*.json"))],
                                capture_output=True, text=True).stdout
        print(f"\n=== {rule}  ({out_dir}/)")
        for line in dict.fromkeys(l.strip() for l in report.splitlines()):
            if line.startswith(("overall step accuracy", "per-step accuracy")) or "FALSE" in line:
                print("  " + line)
    print(f"\nper clip:  python3 evaluate.py {args.dir}/merged-<rule>/tva-output-*.json --verbose")


if __name__ == "__main__":
    main()
