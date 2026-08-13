#!/usr/bin/env python3
"""Iterate on SOP wording against ONE clip, with a private pair of file ids per variant.

The problem this solves. The pipeline receives its inputs as a single flat list of
file ids and pairs a video with its SOP by matching stems, so the obvious naming —
`1_fail_2_pass_3_pass_E.mp4` + `1_fail_2_pass_3_pass_E.txt` — gives every variant
of every SOP the SAME id on the platform. Consequences, all of which cost us time:

  * Two runs on one clip cannot overlap. The second upload replaces the first's
    SOP, and because content is resolved at RUN time rather than submit time, the
    first run reads text it was never launched with. Iteration goes serial at
    ~18 minutes a turn.
  * `1_fail_2_pass_3_pass_E.txt` on the platform means nothing on its own. Which
    of five variants is it? Only the local git state says, and only if nothing
    has been edited since.

So this script derives the pair stem from BOTH the clip and the SOP file:

    data/clips/1_fail_2_pass_3_pass_E.mp4  +  sop/oring-discriminative.txt
      -> 1_fail_2_pass_3_pass_E-oring-discriminative.mp4
      -> 1_fail_2_pass_3_pass_E-oring-discriminative.txt

Stem-matching still holds, every variant is isolated, runs overlap freely, and the
id on the platform says what it is. The cost is one video upload per variant —
`upload_file()` is idempotent on name+bytes, so it is paid once, not per run.

    # launch as many of these AT ONCE as you like
    python3 iterate_sop.py --sop sop/oring-discriminative.txt
    python3 iterate_sop.py --sop sop/oring-focused.txt
    python3 iterate_sop.py --sop sop/oring-last.txt --step-map 2,3,1

    # offline, no API: score everything collected so far, side by side
    python3 iterate_sop.py --compare
"""

import argparse
import glob
import hashlib
import json
import os
import sys

import tva_api as api
from run_tva_example import (CLIP_DIR, BLUEPRINT_DEFAULT, load_sop, resolve_clip,
                             sop_text, parse_ground_truth, STATUSES)

DEFAULT_CLIP = "1_fail_2_pass_3_pass_E.mp4"
OUT_DIR = "eval_iter"


def variant_stem(clip_path: str, sop_path: str) -> str:
    """`<clip stem>-<sop stem>` — the shared stem both uploads get."""
    clip = os.path.splitext(os.path.basename(clip_path))[0]
    sop = os.path.splitext(os.path.basename(sop_path))[0]
    return f"{clip}-{sop}"


def out_path_for(out_dir: str, stem: str, max_frames: int, max_new_tokens: int) -> str:
    """Config goes in the FILENAME, not just the run.

    Two runs of one SOP at different budgets are different experiments, and the
    5760-vs-8192 inversion on the double-omission clip means the budget is not a
    detail. Encoding it stops the second run silently overwriting the first.
    """
    return os.path.join(out_dir, f"{stem}_f{max_frames}_t{max_new_tokens}.json")


def parse_step_map(spec: str | None, n_steps: int) -> dict[int, int]:
    """`--step-map 2,3,1`: SOP line i describes real step map[i].

    Needed because a variant may REORDER the procedure. sop/oring-last.txt puts
    the o-ring last to test whether the blind spot is about the o-ring or about
    being step 0, and without a map its rows would be scored against the wrong
    ground-truth labels — turning a correct answer into an apparent failure.
    """
    if not spec:
        return {i: i for i in range(1, n_steps + 1)}
    parts = [p.strip() for p in spec.replace(",", " ").split()]
    if len(parts) != n_steps:
        sys.exit(f"--step-map has {len(parts)} entries but the SOP has {n_steps} steps")
    try:
        mapped = [int(p) for p in parts]
    except ValueError:
        sys.exit(f"--step-map must be integers, got: {spec}")
    if sorted(mapped) != list(range(1, n_steps + 1)):
        sys.exit(f"--step-map must be a permutation of 1..{n_steps}, got: {spec}")
    return {i + 1: m for i, m in enumerate(mapped)}


def map_sidecar(out_path: str) -> str:
    """Where a non-identity --step-map is recorded, next to its output.

    Without this, `--compare` would score a REORDERED variant against the wrong
    labels — the exact mistake --step-map exists to prevent, reintroduced one
    command later. The map belongs to the output file, so it is stored with it
    rather than being re-supplied (and possibly mis-remembered) at compare time.
    """
    return out_path + ".map"


def rows_of(payload_path: str) -> tuple[str, list[dict]]:
    doc = json.load(open(payload_path))
    if isinstance(doc, list):
        return "?", doc
    return doc.get("id", "?"), doc.get("results") or []


def score(path: str, step_map: dict[int, int] | None = None) -> dict:
    """Verdicts vs ground truth for one output file. Offline."""
    clip, rows = rows_of(path)
    truth = parse_ground_truth(clip)
    n = len(truth) or 3
    step_map = step_map or {i: i for i in range(1, n + 1)}

    # The parser numbers rows from 0 and may repeat a step when generation loops;
    # prefer the first row carrying a status we recognise.
    by_real: dict[int, str] = {}
    for r in rows:
        raw = r.get("step")
        if not isinstance(raw, int):
            continue
        line = raw + 1                      # 0-based on the wire, 1-based here
        real = step_map.get(line, line)
        status = str(r.get("status") or "").upper()
        if status in STATUSES and real not in by_real:
            by_real[real] = status

    cells = {}
    for step, was_ok in sorted(truth.items()):
        got = by_real.get(step)
        if got is None:
            cells[step] = ("none", False)
        else:
            cells[step] = (got, (got == "PASSED") == was_ok)
    return {"clip": clip, "rows": len(rows), "truth": truth, "cells": cells,
            "empty": not rows}


def compare(out_dir: str) -> None:
    """Print every collected variant side by side. No API calls."""
    paths = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    if not paths:
        sys.exit(f"nothing in {out_dir}/ yet — run a variant first")

    print(f"\n{'variant':58} {'rows':>4}  step1        step2        step3        ok")
    print("-" * 110)
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        side = map_sidecar(p)
        smap = None
        if os.path.exists(side):
            smap = parse_step_map(open(side).read().strip(),
                                  len(parse_ground_truth(name)) or 3)
            name += " *"
        s = score(p, smap)
        cols = []
        for step in sorted(s["truth"]):
            got, ok = s["cells"][step]
            cols.append(f"{got:<9}{'OK' if ok else '!!'}")
        n_ok = sum(1 for _, ok in s["cells"].values() if ok)
        print(f"{name[:58]:58} {s['rows']:>4}  {'  '.join(cols)}  {n_ok}/{len(s['cells'])}")
    print("-" * 110)
    print("* = reordered SOP, scored through its .map sidecar.")
    print("truth comes from the clip filename; 'none' means the run never reported "
          "that step.\nempty results are reasoning overflow, NOT a verdict — see the README.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Iterate SOP wording on one clip, isolating each variant's file ids.")
    ap.add_argument("--sop", help="SOP file; its stem names the variant")
    ap.add_argument("--video", default=DEFAULT_CLIP,
                    help=f"clip stem, filename or path (default: {DEFAULT_CLIP})")
    ap.add_argument("--blueprint", default=BLUEPRINT_DEFAULT)
    ap.add_argument("--max-frames", type=int, default=64)
    ap.add_argument("--max-new-tokens", type=int, default=api.DEFAULT_MAX_NEW_TOKENS,
                    help=f"output budget (default {api.DEFAULT_MAX_NEW_TOKENS}). "
                         "The variants already collected at 8192 were run before "
                         "this default changed — pass --max-new-tokens 8192 to "
                         "compare against them like for like.")
    ap.add_argument("--size", type=int, default=224)
    ap.add_argument("--step-map", help="SOP line -> real step, e.g. 2,3,1 for oring-last")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--compare", action="store_true",
                    help="score everything collected so far and stop — no API calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ids and the SOP, upload nothing")
    args = ap.parse_args()

    if args.compare:
        compare(args.out_dir)
        return
    if not args.sop:
        sys.exit("--sop is required (or pass --compare)")

    video = resolve_clip(args.video, CLIP_DIR)
    if video is None:
        sys.exit(f"no such video: {args.video} — try 'python3 sweep.py --list'")

    steps = load_sop(args.sop)
    stem = variant_stem(video, args.sop)
    step_map = parse_step_map(args.step_map, len(steps))
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = out_path_for(args.out_dir, stem, args.max_frames, args.max_new_tokens)

    print(f"variant   {stem}")
    print(f"  video   {video}")
    print(f"  sop     {args.sop} ({len(steps)} steps)")
    print(f"  ids     {stem}.mp4 + {stem}.txt   <- private to this variant")
    print(f"  out     {out_path}")
    if args.step_map:
        print(f"  map     " + ", ".join(f"line {k} = real step {v}"
                                        for k, v in step_map.items()))
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s}")

    if args.dry_run:
        return

    values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens,
              "size": args.size}
    warning = api.check_token_budget(args.max_frames, args.max_new_tokens,
                                     args.size, sop=args.sop)
    if warning:
        print(f"  ** {warning}")

    print(f"\nuploading under the variant stem ...")
    video_id = api.upload_file(video, rename=f"{stem}.mp4")
    sop_id = api.upload_text(sop_text(steps), f"{stem}.txt")
    print(f"  video file_id={video_id}\n  sop   file_id={sop_id}")

    bundle = api.create_bundle(args.blueprint, f"tva-{stem}"[:60], values)
    print(f"bundle {bundle['id']} status={bundle.get('status')}")
    agent_id = api.run(bundle["id"], video_id, sop_id)
    print(f"run {agent_id}\n  watching /logs (~7 min of model loading first) ...")

    # Never leave a run on the GPU. Dev has ONE, and an abandoned run blocks
    # everyone until it times out. Without this, Ctrl-C killed the client with a
    # traceback and left the job running with nothing watching it — the result was
    # still on the platform, but only recoverable by hand from the agent id.
    try:
        verdict = api.poll_logs(agent_id)
    except KeyboardInterrupt:
        print(f"\ninterrupted. The run is STILL ON THE GPU — pick one:\n"
              f"  cancel : python3 -c \"import tva_api as api; "
              f"api.cancel('{agent_id}')\"\n"
              f"  collect: python3 -c \"import tva_api as api; "
              f"api.fetch_results('{agent_id}', '{out_path}')\"")
        sys.exit(130)
    print(f"\nrun {verdict}")
    payload = api.fetch_results(agent_id, out_path)
    if payload is None:
        sys.exit(f"no results — full log: {api.agents_base()}/instances/{agent_id}/logs")
    print(f"  saved {out_path} ({len(payload)} bytes, "
          f"md5 {hashlib.md5(payload).hexdigest()})")
    if args.step_map:
        open(map_sidecar(out_path), "w").write(args.step_map.strip() + "\n")
        print(f"  saved {map_sidecar(out_path)} so --compare scores it correctly")

    s = score(out_path, step_map)
    print()
    if s["empty"]:
        print("  results:[] — generation ended inside the reasoning block. This is "
              "NOT a verdict\n  on the SOP; retry at a lower --max-new-tokens before "
              "concluding anything.")
    else:
        for step, (got, ok) in sorted(s["cells"].items()):
            want = "PASS" if s["truth"][step] else "FAIL"
            print(f"  step {step}: truth={want:4} got={got:8} "
                  f"{'OK' if ok else '<-- WRONG'}")
    print(f"\ncompare every variant:  python3 iterate_sop.py --compare")


if __name__ == "__main__":
    main()
