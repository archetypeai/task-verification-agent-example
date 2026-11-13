#!/usr/bin/env python3
"""Regenerate the per-clip paired SOPs, or verify they have not drifted.

    python3 sop/paired/regenerate.py                       # write from oring-numbered.txt
    python3 sop/paired/regenerate.py --check               # verify only, non-zero if stale
    python3 sop/paired/regenerate.py --sop my-sop.txt   # pair a different SOP

These files are DERIVED. Editing one by hand makes it silently disagree with its
source, and because the pipeline pairs a video to its SOP by stem, the wrong text
would be used for exactly one clip — a difference easy to miss in a sweep.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sop", default=os.path.join("sop", "oring-numbered.txt"),
                    help="source SOP to copy (default: sop/oring-numbered.txt)")
    ap.add_argument("--clips", default=os.path.join("data", "clips"))
    ap.add_argument("--check", action="store_true",
                    help="verify only; exit non-zero if any copy has drifted")
    args = ap.parse_args()

    src = os.path.join(REPO, args.sop)
    if not os.path.exists(src):
        sys.exit(f"no such SOP: {args.sop}")
    body = open(src).read()

    clips = sorted(glob.glob(os.path.join(REPO, args.clips, "*.mp4")))
    if not clips:
        sys.exit(f"no clips found in {args.clips}")

    stale, written = [], []
    for clip in clips:
        stem = os.path.splitext(os.path.basename(clip))[0]
        dst = os.path.join(HERE, f"{stem}.txt")
        current = open(dst).read() if os.path.exists(dst) else None
        if current == body:
            continue
        if args.check:
            stale.append(f"{stem}.txt" + ("" if current is not None else " (missing)"))
        else:
            open(dst, "w").write(body)
            written.append(f"{stem}.txt")

    if args.check:
        if stale:
            print(f"STALE against {args.sop}:")
            for s in stale:
                print(f"  {s}")
            sys.exit(1)
        print(f"all {len(clips)} paired SOPs match {args.sop}")
        return
    print(f"{len(clips)} paired SOPs from {args.sop}; "
          f"{len(written) or 'none'} rewritten"
          + (": " + ", ".join(written) if written else ""))


if __name__ == "__main__":
    main()
