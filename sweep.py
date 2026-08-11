#!/usr/bin/env python3
"""Run TVA across several labelled clips, one at a time, and save each output.

A single run is ~13 minutes of job time (754-814 s observed) and dev has ONE GPU
that serializes jobs, so this is deliberately sequential: submitting concurrently
produces queue waits rather than parallelism — 796 s observed for one clip that
queued behind another. Twelve clips is therefore 2.5-3 hours of wall clock. Budget
accordingly, and prefer --clips to pick a subset.

Almost all of that is cold start: ~7 minutes of model download and load happens on
EVERY run and is never cached, so the marginal cost of one more clip in a sweep is
much lower than the first.

    python3 sweep.py --list                     # what is on disk, with labels

    # clip stems, SPACE-separated (commas work too). Stems come from --list.
    python3 sweep.py --clips 1_pass_2_pass_3_pass_A 1_pass_2_pass_3_fail_A

    python3 sweep.py --all                      # every clip in data/clips
    python3 sweep.py --all --max-frames 16      # the blueprint default, for contrast

Outputs land in eval_out/ as one JSONL per clip, then:

    python3 evaluate.py eval_out/*.jsonl

WHY A SEPARATE SCRIPT from run_tva_example.py: that one is the readable
end-to-end path for a single clip and prints everything. This one is the batch
path — it keeps going when a clip fails, records what happened, and never leaves
a run orphaned on the GPU. If you interrupt it, it cancels the in-flight run
before exiting, because an abandoned run blocks everyone else on dev.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time

import tva_api as api
from run_tva_example import (BLUEPRINT_DEFAULT, DEFAULT_SOP_PATH, load_sop,
                            parse_ground_truth, upload_sop)

OUT_DIR = "eval_out"


def label_str(clip: str) -> str:
    truth = parse_ground_truth(clip)
    return "/".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(truth.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clips", nargs="+",
                    help="clip stems or paths, SPACE-separated (commas also "
                         "accepted): --clips 1_pass_2_pass_3_pass_A "
                         "1_pass_2_pass_3_fail_A")
    ap.add_argument("--all", action="store_true", help="every mp4 in data/clips")
    ap.add_argument("--dir", default="data/clips")
    ap.add_argument("--list", action="store_true",
                    help="list clips and their labels, then stop — no API calls")
    ap.add_argument("--blueprint", default=BLUEPRINT_DEFAULT,
                    help=f"blueprint id or key (default: {BLUEPRINT_DEFAULT}). "
                         f"The key 'tva' currently resolves to a blueprint whose "
                         f"sink cannot be instantiated — see the README.")
    ap.add_argument("--max-frames", type=int, default=64)
    ap.add_argument("--max-new-tokens", type=int, default=8192,
                    help="output budget (default 8192). At 2048 a clip with a "
                         "skipped step exhausted the budget inside the model's "
                         "<think> block and returned results:[] — see the README.")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--sop", metavar="FILE", default=DEFAULT_SOP_PATH,
                    help=f"SOP steps, one per line (default: {DEFAULT_SOP_PATH})")
    args = ap.parse_args()

    available = sorted(glob.glob(os.path.join(args.dir, "*.mp4")))
    if args.list:
        print(f"{len(available)} clip(s) in {args.dir}\n")
        for p in available:
            stem = os.path.splitext(os.path.basename(p))[0]
            print(f"  {stem:<32s} {label_str(stem)}")
        # Worth stating: step 2 is `pass` in every clip in this set, so its
        # accuracy is not evidence of anything — "always PASSED" scores 100%.
        steps = {}
        for p in available:
            for k, v in parse_ground_truth(p).items():
                steps.setdefault(k, set()).add(v)
        print()
        for k, vals in sorted(steps.items()):
            if len(vals) == 1:
                print(f"  NOTE: step {k} is always "
                      f"{'pass' if vals.pop() else 'fail'} in this set — no "
                      f"negative examples, so its accuracy proves nothing.")
        return

    if args.all:
        clips = available
    elif args.clips:
        # Space-separated is the documented form (nargs="+"), but commas are an
        # easy assumption to make, so accept "a,b" and "a b" alike rather than
        # failing on a plausible guess.
        requested = [c for arg in args.clips for c in arg.split(",") if c.strip()]
        clips = []
        for c in requested:
            c = c.strip()
            p = c if os.path.exists(c) else os.path.join(args.dir, f"{c}.mp4")
            if not os.path.exists(p):
                sys.exit(f"no such clip: {c}\n"
                         f"  run 'python3 sweep.py --list' to see the stems")
            clips.append(p)
    else:
        sys.exit("pass --clips <stems...> or --all (or --list to see what there is)")

    steps = load_sop(args.sop or DEFAULT_SOP_PATH)

    os.makedirs(args.out_dir, exist_ok=True)
    values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens}

    bp = api.get_blueprint(args.blueprint)
    inert = api.inert_values(bp["document"], values)
    print(f"blueprint {bp['id']} (key={bp['blueprint_key']}, active={bp['is_active']})")
    if inert:
        print(f"  WARNING: {inert} is not wired and will be silently ignored.")
    sink_warning = api.check_sink(bp["document"])
    if sink_warning:
        # Refuse rather than warn: a sweep would repeat the same doomed cold
        # start once per clip, which is how 40 GPU-minutes get spent on nothing.
        sys.exit(f"REFUSING TO RUN: {sink_warning}")
    print(f"values: {json.dumps(values)}")
    print(f"{len(clips)} clip(s), sequential, ~13 min each "
          f"-> ~{len(clips) * 13} min total, plus queue if the GPU is busy\n")

    # One upload for the whole sweep: every clip is checked against the SAME
    # procedure, so the SOP is uploaded once and its file_id reused.
    sop_id = upload_sop(args.sop, steps)
    print(f"SOP uploaded as {sop_id}\n")
    log = []
    for i, path in enumerate(clips, 1):
        stem = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(args.out_dir, f"tva-output-{stem}.jsonl")
        if os.path.exists(out_path):
            print(f"[{i}/{len(clips)}] {stem} — already have "
                  f"{out_path}, skipping")
            continue
        print(f"[{i}/{len(clips)}] {stem}  ({label_str(stem)})")
        t0 = time.time()
        agent_id = None
        try:
            video_id = api.upload_file(path)
            bundle = api.create_bundle(args.blueprint, f"TVA sweep {stem}", values)
            agent_id = api.run(bundle["id"], video_id, sop_id)
            print(f"  agent_id={agent_id}")
            verdict = api.poll_logs(agent_id, quiet=True)
            payload = api.fetch_results(agent_id, out_path)
            n = 0
            if payload:
                recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines() if l.strip()]
                n = sum(len(r.get("results", [])) for r in recs)
            print(f"  {verdict} in {time.time() - t0:.0f}s — {n} step(s), "
                  f"{len(payload or b'')} bytes, "
                  f"md5 {hashlib.md5(payload or b'').hexdigest()[:8]}")
            log.append({"clip": stem, "agent_id": agent_id, "verdict": verdict,
                        "steps": n, "seconds": round(time.time() - t0)})
        except KeyboardInterrupt:
            # Never leave a run on the GPU. Dev has one, and an abandoned run
            # blocks everyone until it times out.
            if agent_id:
                print(f"\n  interrupted — cancelling {agent_id}")
                api.cancel(agent_id)
            sys.exit(130)
        except SystemExit as e:
            print(f"  FAILED: {e}")
            log.append({"clip": stem, "agent_id": agent_id, "verdict": "error",
                        "error": str(e)})

    with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as fh:
        json.dump(log, fh, indent=2)
    print(f"\nwrote {args.out_dir}/sweep-log.json")
    print(f"score it:  python3 evaluate.py {args.out_dir}/*.jsonl")


if __name__ == "__main__":
    main()
