#!/usr/bin/env python3
"""Score TVA outputs against the ground truth encoded in each clip's filename.

Offline and stdlib-only: no API key, no network, no GPU. A run happens once and
costs ~9 minutes of a shared GPU, but you will read its numbers many times, so
scoring is a separate free step that reads committed files.

    python3 evaluate.py reference/*.jsonl          # score every committed run
    python3 evaluate.py tva-output-1_pass_2_pass_3_pass_A.jsonl
    python3 evaluate.py reference/*.jsonl --verbose   # per-step reasons too

GROUND TRUTH comes from the filename: `1_pass_2_pass_3_fail_A.mp4` means step 1
correct, step 2 correct, step 3 not. The trailing letter is a take id. That makes
the labels visible in `ls` and impossible to desynchronise from the media, which
is worth more than it sounds — but it is a BINARY label, and TVA emits THREE
statuses, so the mapping is the interesting part:

    PASSED   -> the step was performed correctly
    FAILED   -> the step was attempted and done wrong
    MISSING  -> the step never happened at all

A `fail` label collapses FAILED and MISSING together. Both are the right VERDICT
for a `fail` clip, so accuracy treats them as equivalent — but they are different
CLAIMS about the video, and only one of them fits a given clip. This script
reports the split separately rather than hiding it inside one accuracy number.

For THESE clips the failures are all OMISSIONS, confirmed by watching them
frame by frame: in `1_fail` the cap goes straight to the manifold with no o-ring
fitted, and in `3_fail` the wrench never enters the frame at all. Nothing is
attempted and botched. So `MISSING` is the status that matches the video and
`FAILED` is the one worth a second look — which is the opposite of what the
label word "fail" suggests, and the reason this distinction is reported at all.

WHAT THIS DOES NOT DO: score timestamps. Unlike MGA, where the whole output is a
temporal segmentation and IoU is the natural metric, TVA's timestamps are
supporting evidence for a verdict. There is no per-step ground-truth interval in
these filenames to compare against, so the intervals are reported and sanity
checked, never scored. Do not read the absence of an IoU column as TVA localising
perfectly; read it as unmeasured.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

STATUSES = ("PASSED", "FAILED", "MISSING")


def parse_ground_truth(name: str) -> dict[int, bool]:
    """{step: was_performed_correctly} from `1_pass_2_fail_3_pass_A`, 1-based."""
    stem = os.path.splitext(os.path.basename(name))[0]
    return {int(m.group(1)): m.group(2).lower() == "pass"
            for m in re.finditer(r"(\d+)_(pass|fail)", stem, re.IGNORECASE)}


def clip_name(path: str, rec: dict) -> str:
    """The clip a record describes.

    Prefer the record's own `id`, which the platform sets from the input
    filename, and fall back to the output filename. Either carries the labels.
    """
    rid = str(rec.get("id") or "")
    if parse_ground_truth(rid):
        return rid
    return os.path.basename(path)


def load(paths: list[str]) -> list[tuple[str, dict]]:
    """Read every JSONL record from every path, as (clip, record) pairs."""
    out = []
    for path in paths:
        raw = open(path, "rb").read().decode(errors="replace")
        records = [json.loads(l) for l in raw.splitlines() if l.strip()]
        if not records:
            # A run can complete and return nothing. That is a result, not a
            # read error, so surface it rather than skipping the file silently.
            print(f"  {os.path.basename(path)}: EMPTY (0 records, {len(raw)} bytes)"
                  f" — the run completed and produced no verification report")
            continue
        for rec in records:
            out.append((clip_name(path, rec), rec))
    return out


def by_step(rec: dict) -> dict[int, dict]:
    """{step_number: row}, normalising the parser's step numbering to 1-based.

    The design doc says `step: <int>` and does not say where it starts. It is
    0-based — verified on real runs, and the same as MGA's parser. Detected here
    rather than assumed, because an off-by-one produces plausible wrong
    alignments instead of an error: a perfect 3-step run misreads as two hits
    and a missing third step.
    """
    rows = rec.get("results", []) or []
    nums = [r.get("step") for r in rows if r.get("step") is not None]
    base = min(nums) if nums else 1
    # Only 0 and 1 are credible bases. Anything else means the run reported a
    # subset of steps (e.g. steps 2-3 only), in which case 1-based is right.
    shift = 1 if base == 0 else 0
    return {int(r["step"]) + shift: r for r in rows if r.get("step") is not None}


def score(pairs: list[tuple[str, dict]], verbose: bool = False) -> None:
    n_steps = max((max(parse_ground_truth(c) or {0: 0}) for c, _ in pairs),
                  default=0)
    if not n_steps:
        sys.exit("no ground-truth labels found in any filename or record id")

    hdr = "".join(f"  {'step ' + str(i):^18s}" for i in range(1, n_steps + 1))
    width = 44 + 20 * n_steps
    print("=" * width)
    print(f"{'clip':<42s}{hdr}")
    print("-" * width)

    correct = total = 0
    per_step = {i: [0, 0] for i in range(1, n_steps + 1)}       # [correct, total]
    confusion = {i: {"TP": 0, "TN": 0, "FP": 0, "FN": 0} for i in range(1, n_steps + 1)}
    reason_wrong = []          # verdict right, status wrong (FAILED vs MISSING)
    unreported = []            # a labelled step the run never mentioned
    status_counts = {s: 0 for s in STATUSES}
    no_interval = 0
    intervals = []

    for clip, rec in sorted(pairs):
        truth = parse_ground_truth(clip)
        rows = by_step(rec)
        cells = []
        for step in range(1, n_steps + 1):
            if step not in truth:
                cells.append("—")
                continue
            expected = truth[step]
            row = rows.get(step)
            status = str(row.get("status", "?")).upper() if row else "NOT REPORTED"
            if status in status_counts:
                status_counts[status] += 1
            if row is None:
                unreported.append((clip, step))

            # PASSED is the only status meaning "done correctly"; FAILED and
            # MISSING are the same verdict from different observations.
            predicted = status == "PASSED"
            # A step the run never reported is NOT a correct FAIL detection. It is
            # a missing answer, and crediting it would score an empty output as
            # 100% on every fail-labelled clip — which is exactly what an
            # exhausted reasoning budget produces. Never correct.
            ok = (row is not None) and (predicted == expected)
            correct += ok
            total += 1
            per_step[step][0] += ok
            per_step[step][1] += 1

            c = confusion[step]
            if row is None:
                # Outside the confusion matrix entirely: no verdict was given, so
                # it is neither a true nor a false anything.
                cells.append(f"{'PASS' if expected else 'FAIL'}>none  !!")
                continue
            if expected and predicted:
                c["TP"] += 1
            elif not expected and not predicted:
                c["TN"] += 1
            elif not expected and predicted:
                c["FP"] += 1
            else:
                c["FN"] += 1

            # A `fail` clip is right on the verdict whether the model says FAILED
            # or MISSING, but those are different claims about the video. Every
            # failure in this set is an OMISSION (verified frame by frame), so
            # MISSING fits and FAILED — "attempted, done wrong" — does not.
            if not expected and status == "FAILED":
                reason_wrong.append((clip, step, status))

            ts, te = (row or {}).get("timestamp_start"), (row or {}).get("timestamp_end")
            if row is not None:
                if ts is None or te is None:
                    no_interval += 1
                else:
                    intervals.append((clip, step, float(ts), float(te)))

            cells.append(f"{'PASS' if expected else 'FAIL'}>"
                         f"{status[:7]} {'OK' if ok else '!!'}")
        print(f"{clip[:42]:<42s}" + "".join(f"  {c:^18s}" for c in cells))

    print("-" * width)
    if total:
        print(f"\noverall step accuracy: {correct}/{total} "
              f"({correct / total * 100:.1f}%)")
        print("per-step accuracy:     " + "  |  ".join(
            f"step {i}: {c}/{t} ({(c / t * 100) if t else 0:.0f}%)"
            for i, (c, t) in sorted(per_step.items())))

    print("\nconfusion per step (positive = PASSED = performed correctly):")
    for step, c in sorted(confusion.items()):
        tot = sum(c.values())
        acc = (c["TP"] + c["TN"]) / tot * 100 if tot else 0
        print(f"  step {step}: TP={c['TP']} TN={c['TN']} FP={c['FP']} "
              f"FN={c['FN']}   acc={acc:.0f}%")
        if c["FP"] and not c["FN"]:
            print(f"           all {c['FP']} error(s) are FALSE PASSES — it "
                  f"credits work that was not done correctly")
        elif c["FN"] and not c["FP"]:
            print(f"           all {c['FN']} error(s) are FALSE FAILS — it "
                  f"rejects work that was correct")

    print("\nstatus distribution: " + ", ".join(
        f"{s}={status_counts[s]}" for s in STATUSES))
    if reason_wrong:
        print(f"\nverdict right, reason arguable ({len(reason_wrong)}): every "
              f"failure in this set is an OMISSION —\n  the o-ring is never "
              f"fitted, or the wrench never appears. TVA reported FAILED "
              f"('attempted,\n  done wrong') where MISSING ('never happened') "
              f"is what the video shows:")
        for clip, step, status in reason_wrong:
            print(f"    {clip}  step {step}: {status}")
    if unreported:
        print(f"\nlabelled steps the run never reported ({len(unreported)}):")
        for clip, step in unreported:
            print(f"    {clip}  step {step}")
    if no_interval:
        print(f"\nrows with no timestamps: {no_interval} "
              f"(expected for MISSING — there is no interval to point at)")

    if intervals:
        print("\nintervals reported (NOT scored — no per-step ground-truth "
              "spans exist in these labels):")
        for clip, step, ts, te in intervals:
            flag = ""
            if te < ts:
                flag = "  <-- END BEFORE START"
            elif te == ts:
                flag = "  <-- zero-length"
            print(f"    {clip[:40]:<40s} step {step}: {ts:6.1f} - {te:6.1f}s{flag}")

    if verbose:
        print("\n" + "=" * width)
        print("reasons given")
        print("=" * width)
        for clip, rec in sorted(pairs):
            print(f"\n{clip}")
            for step, row in sorted(by_step(rec).items()):
                print(f"  step {step}  {str(row.get('status')):8} "
                      f"{row.get('reason', '')}")
    print("=" * width)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outputs", nargs="+", help="JSONL output files (globs fine)")
    ap.add_argument("--verbose", action="store_true",
                    help="also print the reason TVA gave for every step")
    args = ap.parse_args()

    # Report a pattern that matched nothing rather than dropping it. A shell that
    # leaves an unmatched glob unexpanded, or a plain typo, would otherwise score
    # whatever DID match and look like a clean run over a smaller set.
    unmatched = [pat for pat in args.outputs if not glob.glob(pat)]
    if unmatched:
        sys.exit(f"matched no files: {', '.join(unmatched)}")
    paths = sorted({p for pat in args.outputs for p in glob.glob(pat)})
    print(f"scoring {len(paths)} file(s)\n")
    pairs = load(paths)
    if not pairs:
        sys.exit("no records to score")
    score(pairs, verbose=args.verbose)


if __name__ == "__main__":
    main()
