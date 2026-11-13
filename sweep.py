#!/usr/bin/env python3
"""Run TVA across several labelled clips, one at a time, and save each output.

A single run is ~10-15 minutes of job time, so this is sequential by default:
twelve clips one after another is 2-3 hours of wall clock. --submit-all queues them
all up front instead (51 min for twelve on production, which ran two at once).
Budget accordingly, and prefer --clips to pick a subset.

Almost all of that is cold start: ~7 minutes of model download and load happens on
EVERY run and is never cached, so the marginal cost of one more clip in a sweep is
much lower than the first.

    python3 sweep.py --list                     # what is on disk, with labels

    # clip stems, SPACE-separated (commas work too). Stems come from --list.
    python3 sweep.py --clips 1_pass_2_pass_3_pass_A 1_pass_2_pass_3_fail_A

    python3 sweep.py --all                      # every clip in data/clips
    python3 sweep.py --all --max-frames 16      # the blueprint default, for contrast

Outputs land in eval_out/ as one JSON file per clip, then:

    python3 evaluate.py eval_out/tva-output-*.json

WHY A SEPARATE SCRIPT from run_tva_example.py: that one is the readable
end-to-end path for a single clip and prints everything. This one is the batch
path — it keeps going when a clip fails, records what happened, and never leaves
a run orphaned on the GPU. If you interrupt it, it cancels the in-flight session
before exiting, because an abandoned run holds a GPU.

Every clip runs as one batch session on ONE deployment for the sweep's setting:
the project's copy of `tva` at --max-frames/--size/--max-new-tokens, its package
and its batch deployment, made once per setting and reused (tva_project.py).
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
import tva_project as project
from background import add_background_flag, maybe_detach
from run_tva_example import (BLUEPRINT_DEFAULT, DEFAULT_SOP_PATH, load_sop,
                            pair_names, parse_ground_truth, resolve_clip, sop_text,
                            upload_sop)

OUT_DIR = "eval_out"


def label_str(clip: str) -> str:
    truth = parse_ground_truth(clip)
    return "/".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(truth.items()))


def upload_pair(args, path: str, stem: str, steps: list[str],
                shared: str | None, suffix: str | None = None) -> tuple[str, str]:
    """Upload one clip's video and SOP, returning (video_id, sop_id).

    ONE function used by BOTH run paths — the sequential loop and --submit-all. An
    earlier version had the pairing logic in the sequential loop only, so
    --submit-all sent `id: null` and the API rejected the run with a 422.

    Naming matches run_tva_example.py via pair_names(), so the two scripts cannot
    disagree about what a run's inputs are called:

      default      <clip>-<sop>.mp4 + <clip>-<sop>.txt   stems match AND name the SOP
      --pair-sop   <clip>.mp4       + <clip>.txt         the clip's stem only
      shared       <clip>.mp4       + <sop>.txt          one SOP object for the sweep

    `shared` is only used for the third case, which a 1:1 run can get away with.
    """
    if shared is not None:
        return api.upload_file(path), shared
    v_name, s_name = pair_names(stem, args.sop, args.pair_sop,
                                os.path.basename(path), suffix=suffix)
    if args.pair_sop:
        video_id = api.upload_file(path)
        committed = os.path.join("sop", "paired", f"{stem}.txt")
        if os.path.exists(committed) and open(committed).read() == sop_text(steps):
            return video_id, api.upload_file(committed)
        return video_id, api.upload_text(sop_text(steps), s_name)
    return (api.upload_file(path, rename=v_name),
            api.upload_text(sop_text(steps), s_name))


def submit_all(args, rec: dict, sop_id: str | None, clips: list[str],
               steps: list[str]) -> None:
    """Submit every run before collecting any, then poll them in order.

    Sequential submission leaves the GPU unclaimed between clips, and another job
    can take the slot. Submitting up front holds the positions, and production ran
    two at once, so it also roughly halves the wall clock.
    And it removes the safety property of the sequential path, where a
    misconfiguration costs one run rather than all of them — so the sequential
    path stays the default.
    """
    pending = []
    for i, path in enumerate(clips, 1):
        stem = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(args.out_dir, f"tva-output-{stem}.json")
        if os.path.exists(out_path) and not args.fresh:
            print(f"[{i}/{len(clips)}] {stem} — already have {out_path}, skipping "
                  f"(--fresh to re-run)")
            continue
        video_id, clip_sop_id = upload_pair(args, path, stem, steps, sop_id,
                                            suffix=args._suffix)
        session_id = api.open_session(rec["batch"], video_id, clip_sop_id)["id"]
        project.record_session(session_id, setting=rec["key"], kind="batch",
                               clip=stem, out=out_path)
        print(f"[{i}/{len(clips)}] submitted {stem:<32s} {session_id}")
        pending.append((stem, session_id, out_path))

    print(f"\n{len(pending)} run(s) queued; the platform decides how many run at "
          f"once. Collecting in submission order.\n"
          f"Interrupting now leaves them running — cancel with:\n"
          f"  python3 sweep.py --cancel-all\n")
    log = []
    for stem, session_id, out_path in pending:
        t0 = time.time()
        verdict = api.poll_session(session_id, quiet=True)
        payload = api.fetch_results(session_id, out_path)
        n = 0
        if payload:
            recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines() if l.strip()]
            n = sum(len(r.get("results", [])) for r in recs)
        print(f"  {stem:<32s} {verdict} in {time.time() - t0:.0f}s — {n} step(s), "
              f"{len(payload or b'')} bytes")
        log.append({"clip": stem, "session_id": session_id, "verdict": verdict,
                    "steps": n})
    with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as fh:
        json.dump(log, fh, indent=2)
    print(f"\nscore it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


def collect(out_dir: str, setting: str | None = None) -> None:
    """Download the output of every finished session, after the fact.

    The recovery path, and the reason it exists: the runs live on the PLATFORM,
    not in the client. A client that dies — closed laptop, lost network, Ctrl-C,
    an exhausted retry budget — abandons results that are already computed and
    that do not expire. This walks the sessions recorded in state/lifecycle.json
    (each is recorded the moment it exists) and harvests whatever is ready, so a
    sweep never has to be re-run because nobody was listening.

    Safe to run repeatedly: it skips clips already on disk and ignores sessions
    that are still going.
    """
    rows = {sid: x for sid, x in project.sessions().items()
            if x.get("kind") == "batch" and (setting is None or x.get("setting") == setting)}
    if not rows:
        print("no recorded tva sessions" + (f" for {setting}" if setting else ""))
        return

    os.makedirs(out_dir, exist_ok=True)
    saved = skipped = pending = empty = dead = 0
    for sid, x in sorted(rows.items(), key=lambda r: str(r[1].get("created_at"))):
        stem = x.get("clip") or sid
        out_path = os.path.join(out_dir, f"tva-output-{stem}.json")
        if os.path.exists(out_path):
            skipped += 1
            continue
        code, ses = api.request("GET", api.sessions_url(sid), allow_status=True)
        if code == 404:
            continue                                 # deleted by cleanup.py
        if code != 200:
            sys.exit(f"GET {api.sessions_url(sid)} failed ({code}): {ses}")
        status = ses.get("status")
        if status not in api.TERMINAL:
            pending += 1
            print(f"  {sid}  {status:9}  {stem:<32s} still in flight, no results yet")
            continue
        if status != "completed":
            # "No results" means two different things, and conflating them tells
            # you to keep waiting for a run that is already over. A failed or
            # cancelled session will never produce output.
            dead += 1
            print(f"  FAILED  {stem}  ({sid}, {status})")
            print(f"      it will not produce output. cause: {str(ses.get('error'))[:200]}")
            continue
        payload = api.fetch_results(sid, out_path)
        if payload is None:
            dead += 1
            continue
        recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines()
                if l.strip()]
        n = sum(len(r.get("results", [])) for r in recs)
        if n == 0:
            empty += 1
        print(f"  {sid}  {status:9}  {stem:<32s} {n} verdict(s), "
              f"{len(payload)} bytes -> {out_path}")
        saved += 1

    print(f"\nsaved {saved}, already had {skipped}, still in flight {pending}"
          + (f", EMPTY {empty} (reasoning overflow — see the README)" if empty else "")
          + (f", FAILED {dead} (never will produce output)" if dead else ""))
    if saved or skipped:
        print(f"score it:  python3 evaluate.py {out_dir}/tva-output-*.json")


def cancel_all() -> None:
    """Cancel every recorded session still going. The escape hatch for --submit-all."""
    live = 0
    for sid in project.sessions():
        code, ses = api.request("GET", api.sessions_url(sid), allow_status=True)
        if code == 200 and ses.get("status") not in api.TERMINAL:
            api.cancel_session(sid)
            live += 1
            print(f"cancelled {sid} ({ses.get('status')})")
    if not live:
        print("no session still going")


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
                    help=f"the blueprint to copy into the project, by id or key "
                         f"(default: {BLUEPRINT_DEFAULT})")
    ap.add_argument("--max-frames", type=int, default=64)
    ap.add_argument("--size", type=int, default=224,
                    help="each frame is resized to size x size (blueprint default "
                         "224). Raising it recovers spatial detail — a ~10 mm "
                         "o-ring is a handful of pixels at 224 — but visual tokens "
                         "grow as (size/32)^2 PER temporal step and share the 8192 "
                         "context, so more size forces fewer frames. Source "
                         "resolution (null) is unreachable: 1080p costs ~3600 "
                         "tokens per step.")
    ap.add_argument("--max-new-tokens", type=int, default=None,
                    help=f"output budget (default {api.DEFAULT_MAX_NEW_TOKENS}). "
                         "BIGGER IS NOT BETTER: the failure mode is repetition, and "
                         "on the double-omission clip 8192 returned results:[] where "
                         "5760 returned correct verdicts. At 2048 the budget is "
                         "exhausted inside the <think> block — see the README.")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--submit-all", action="store_true",
                    help="submit every run UP FRONT, then collect results, "
                         "instead of waiting for each before starting the next. "
                         "Does not make each run faster, but it "
                         "holds your place in the queue so another org's job "
                         "cannot slip in between your clips. The cost: a "
                         "misconfiguration burns every run instead of the first, "
                         "and this example has hit two such failures already.")
    ap.add_argument("--fresh", action="store_true",
                    help="re-run every clip, REPLACING any output already on disk. "
                         "Without this, --all skips them — which is what makes an "
                         "interrupted sweep resumable, and why a fresh clone of this "
                         "repo runs nothing (the twelve outputs are committed). "
                         "Deliberately NOT --force: that flag already exists in "
                         "run_tva_example.py meaning 'override the sink guardrail', "
                         "and one name for two behaviours across sibling scripts is a "
                         "trap.")
    ap.add_argument("--shared-sop", action="store_true",
                    help="upload ONE SOP object for the whole sweep, named after the "
                         "SOP file, with PLAIN ids and no uniqueness tail. Cheapest — "
                         "the idempotent skip fires so no video is re-uploaded — but "
                         "stems do not match, the platform cannot later tell you which "
                         "SOP a run used, and a concurrent user writes to the same ids "
                         "and can destroy your queued runs. SOLO USE ONLY.")
    ap.add_argument("--pair-sop", action="store_true",
                    help="upload one SOP per clip named <clip-stem>.txt, from "
                         "sop/paired/ when present. The pipeline pairs video and "
                         "text by stem because inputs arrive as one flat list of "
                         "file ids, so a shared SOP name is only safe for 1:1 runs. "
                         "Also removes the risk of concurrent runs clobbering a "
                         "shared SOP object.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan — the setting, clips, which are "
                         "already done, estimated time — and stop. No API calls.")
    ap.add_argument("--sop", metavar="FILE", default=DEFAULT_SOP_PATH,
                    help=f"SOP steps, one per line (default: {DEFAULT_SOP_PATH})")
    ap.add_argument("--collect", action="store_true",
                    help="download the output of every recorded session that "
                         "has finished, and stop. "
                         "Use after a disconnect, a closed laptop or a Ctrl-C: the "
                         "runs continue on the platform and their results do not "
                         "expire, so nothing needs re-running. Safe to repeat.")
    ap.add_argument("--setting", metavar="KEY",
                    help="with --collect, restrict to one setting's sessions "
                         "(e.g. tva-f64-s224-t5760).")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel every recorded session still going, and stop. The "
                         "escape hatch after --submit-all, since an abandoned run "
                         "blocks the shared GPU.")
    add_background_flag(ap, default_log="logs/sweep.log")
    args = ap.parse_args()
    maybe_detach(args)

    # ONE suffix for the whole sweep, so every upload from this batch shares it and
    # `<clip>-<sop>-<this timestamp>` greps as a unit in an org-wide file list. Clip
    # stems already differ, so per-clip uniqueness is preserved.
    args._suffix = api.run_suffix()

    # --fresh REPLACES outputs, and reference/ holds the committed results the README
    # is scored from.
    if args.fresh and os.path.normpath(args.out_dir).split(os.sep)[0] == "reference":
        sys.exit(f"refusing --fresh into {args.out_dir}: reference/ is committed "
                 f"evidence, not a scratch directory.\n"
                 f"  use a new directory instead:  --out-dir eval_rerun")

    if args.collect:
        collect(args.out_dir, args.setting)
        return

    if args.cancel_all:
        cancel_all()
        return

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
            p = resolve_clip(c.strip(), args.dir)
            if p is None:
                sys.exit(f"no such clip: {c.strip()}\n"
                         f"  looked for it as a path, and in {args.dir}/ with and "
                         f"without a video extension.\n"
                         f"  run 'python3 sweep.py --list' to see what is there.")
            clips.append(p)
    else:
        sys.exit("pass --clips <stems...> or --all (or --list to see what there is)")

    steps = load_sop(args.sop or DEFAULT_SOP_PATH)

    # Default to a budget that has actually PRODUCED ANSWERS, not to the ceiling.
    # See api.DEFAULT_MAX_NEW_TOKENS for why the ceiling is the wrong default.
    if args.max_new_tokens is None:
        ceiling = api.max_usable_new_tokens(args.max_frames, args.size, sop=args.sop)
        args.max_new_tokens = min(api.DEFAULT_MAX_NEW_TOKENS, ceiling)
        print(f"max_new_tokens not set -> using {args.max_new_tokens} "
              f"(the ceiling here is {ceiling}; a bigger budget is more room to "
              f"LOOP, not to reason — see api.DEFAULT_MAX_NEW_TOKENS).\n"
              f"  Pass --max-new-tokens {ceiling} to use the whole window instead.")
    values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens,
              "size": args.size}

    if args.dry_run:
        # Resuming matters here: a sweep is hours long and gets interrupted, so
        # show what would be skipped rather than making the caller guess.
        todo, done = [], []
        for path in clips:
            stem = os.path.splitext(os.path.basename(path))[0]
            out = os.path.join(args.out_dir, f"tva-output-{stem}.json")
            (todo if args.fresh or not os.path.exists(out)
             else done).append(stem)
        print(f"setting    {project.setting_key(values, args.blueprint)}  "
              f"(a copy of {args.blueprint})")
        print(f"values     {json.dumps(values)}")
        # Show the ACTUAL ids for a representative clip, not a guess: the naming
        # differs per mode, and a --dry-run that prints the wrong name is worse than
        # one that prints nothing.
        sample = os.path.splitext(os.path.basename((todo or done or ["<clip>"])[0]))[0]
        if args.shared_sop:
            sample_v, sample_s = f"{sample}.mp4", os.path.basename(args.sop)
        else:
            sample_v, sample_s = pair_names(sample, args.sop, args.pair_sop,
                                            suffix=args._suffix)
        print(f"SOP        {args.sop} ({len(steps)} steps)")
        print(f"inputs     {sample_v} + {sample_s}"
              f"{'   <- stems match' if sample_v.rsplit('.',1)[0] == sample_s.rsplit('.',1)[0] else '   <- stems DIFFER'}")
        if args.shared_sop:
            print(f"           ** PLAIN NAMES, no uniqueness tail. Cheap — the "
                  f"idempotent skip fires, so\n"
                  f"           ** no video is re-uploaded — but ANOTHER USER running "
                  f"these clips writes\n"
                  f"           ** to the same ids and can destroy your queued runs. "
                  f"Solo use only.")
        else:
            print(f"           the -<UTC>-<hex> tail is generated per sweep, so the "
                  f"REAL run will differ.\n"
                  f"           It keeps concurrent users from overwriting each "
                  f"other's inputs.")
        print("agent      ONE copy, package and batch deployment for the setting, "
              "reused; a session per clip")
        print(f"outputs    {args.out_dir}/tva-output-<clip>.json")
        if done:
            print(f"\nalready done, would be SKIPPED ({len(done)}):")
            for stem in done:
                print(f"  {stem:<32s} {label_str(stem)}")
        # --submit-all changes WHEN runs are submitted. How many execute at once is
        # the platform's call (two at once on production).
        mode = ("all submitted up front, then collected in order — the platform "
                "decides how many run at once" if args.submit_all
                else "submitted one at a time")
        print(f"\nwould run ({len(todo)}), {mode}, ~13 min each "
              f"-> ~{len(todo) * 13} min (~{len(todo) * 13 / 60:.1f} h):")
        for i, stem in enumerate(todo, 1):
            print(f"  {i:2}. {stem:<32s} {label_str(stem)}")
        return

    os.makedirs(args.out_dir, exist_ok=True)
    # Every clip already done means nothing will run. Say so before building an
    # agent: the likely cause is an out-dir holding results from another
    # environment or blueprint, which the skip check cannot tell apart.
    if not args.fresh and all(
            os.path.exists(os.path.join(
                args.out_dir,
                f"tva-output-{os.path.splitext(os.path.basename(p))[0]}.json"))
            for p in clips):
        sys.exit(f"nothing to run: all {len(clips)} clip(s) already have an output "
                 f"in {args.out_dir}/.\n"
                 f"  To run them again, write elsewhere (--out-dir eval_rerun) "
                 f"or replace them (--fresh).")
    bp = api.get_blueprint(args.blueprint)
    inert = api.inert_values(bp["document"], values)
    print(f"blueprint {bp['id']} (key={bp['blueprint_key']}, active={bp['is_active']})")
    if inert:
        print(f"  WARNING: {inert} is not wired and will be silently ignored.")
    budget_warning = api.check_token_budget(args.max_frames, args.max_new_tokens, args.size, sop=args.sop)
    if budget_warning:
        print(f"  ** {budget_warning}")
    sink_warning = api.check_sink(bp["document"])
    if sink_warning:
        # Refuse rather than warn: a sweep would repeat the same doomed cold
        # start once per clip, which is how 40 GPU-minutes get spent on nothing.
        sys.exit(f"REFUSING TO RUN: {sink_warning}")
    print(f"values: {json.dumps(values)}")
    mode = "all submitted up front" if args.submit_all else "sequential"
    print(f"{len(clips)} clip(s), {mode}, ~13 min each "
          f"-> ~{len(clips) * 13} min total, plus queue if the GPU is busy\n")

    # One upload for the whole sweep: every clip is checked against the SAME
    # procedure, so the SOP is uploaded once and its file_id reused.
    sop_id = None
    if args.shared_sop:
        # One SOP object for the whole sweep. Cheapest, and stems do NOT match — fine
        # for 1:1 runs, but it means the platform
        # cannot tell you afterwards which SOP a given run was given.
        sop_id = upload_sop(args.sop, steps)
        print(f"SOP uploaded as {sop_id} (ONE object shared by all clips; "
              f"stems do NOT match)")
    elif args.pair_sop:
        print("pairing mode: one SOP per clip, named <clip-stem>.txt "
              "(every SOP variant collides on this name)")
    else:
        print("default: one pair per clip, named <clip-stem>-"
              f"{os.path.splitext(os.path.basename(args.sop))[0]}.{{mp4,txt}} "
              "— stems match and record the SOP")

    # ONE agent for the whole sweep — the setting's copy, package and batch
    # deployment — and a session per clip.
    #
    # The two things that vary per run — the video and the SOP — are both source
    # connector inputs, supplied per session. So there is nothing a per-clip copy
    # or deployment could hold that differs. This is the same property the
    # README's Stage 2 describes from the other direction: because the SOP travels
    # with the session rather than the blueprint, one agent serves every SOP — and
    # a fortiori every clip under one SOP.
    rec = project.agent(values, "batch", source=args.blueprint)
    print(f"agent {rec['key']}: copy {rec['blueprint']}, package {rec['package']}, "
          f"deployment {rec['batch']} (reused for all {len(clips)} runs)\n")
    if args.submit_all:
        submit_all(args, rec, sop_id, clips, steps)
        return

    log = []
    for i, path in enumerate(clips, 1):
        stem = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(args.out_dir, f"tva-output-{stem}.json")
        if os.path.exists(out_path) and not args.fresh:
            print(f"[{i}/{len(clips)}] {stem} — already have "
                  f"{out_path}, skipping (--fresh to re-run)")
            continue
        print(f"[{i}/{len(clips)}] {stem}  ({label_str(stem)})")
        t0 = time.time()
        session_id = None
        try:
            video_id, clip_sop_id = upload_pair(args, path, stem, steps, sop_id,
                                                suffix=args._suffix)
            session_id = api.open_session(rec["batch"], video_id, clip_sop_id)["id"]
            project.record_session(session_id, setting=rec["key"], kind="batch",
                                   clip=stem, out=out_path)
            print(f"  session_id={session_id}")
            verdict = api.poll_session(session_id, quiet=True)
            payload = api.fetch_results(session_id, out_path)
            n = 0
            if payload:
                recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines() if l.strip()]
                n = sum(len(r.get("results", [])) for r in recs)
            print(f"  {verdict} in {time.time() - t0:.0f}s — {n} step(s), "
                  f"{len(payload or b'')} bytes, "
                  f"md5 {hashlib.md5(payload or b'').hexdigest()[:8]}")
            log.append({"clip": stem, "session_id": session_id, "verdict": verdict,
                        "steps": n, "seconds": round(time.time() - t0)})
        except KeyboardInterrupt:
            # Never leave a run on the GPU: an abandoned run holds it until it times out.
            if session_id:
                print(f"\n  interrupted — cancelling {session_id}")
                api.cancel_session(session_id)
            sys.exit(130)
        except SystemExit as e:
            print(f"  FAILED: {e}")
            log.append({"clip": stem, "session_id": session_id, "verdict": "error",
                        "error": str(e)})

    with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as fh:
        json.dump(log, fh, indent=2)
    print(f"\nwrote {args.out_dir}/sweep-log.json")
    print(f"score it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


if __name__ == "__main__":
    main()
