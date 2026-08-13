#!/usr/bin/env python3
"""Run TVA across several labelled clips, one at a time, and save each output.

A single run is ~13 minutes of job time (754-1614 s observed) and dev has ONE GPU
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

Outputs land in eval_out/ as one JSON file per clip, then:

    python3 evaluate.py eval_out/tva-output-*.json

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
import re
import sys
import time

import tva_api as api
from run_tva_example import (BLUEPRINT_DEFAULT, DEFAULT_SOP_PATH, load_sop,
                            pair_names, parse_ground_truth, resolve_clip, sop_text,
                            upload_sop)

OUT_DIR = "eval_out"


def label_str(clip: str) -> str:
    truth = parse_ground_truth(clip)
    return "/".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(truth.items()))


def upload_pair(args, path: str, stem: str, steps: list[str],
                shared: str | None) -> tuple[str, str]:
    """Upload one clip's video and SOP, returning (video_id, sop_id).

    ONE function used by BOTH run paths — the sequential loop and --submit-all. An
    earlier version had the pairing logic in the sequential loop only, so
    --submit-all sent `id: null` and the API rejected the run with a 422.

    Naming matches run_tva_example.py via pair_names(), so the two scripts cannot
    disagree about what a run's inputs are called:

      default      <clip>-<sop>.mp4 + <clip>-<sop>.txt   stems match AND name the SOP
      --pair-sop   <clip>.mp4       + <clip>.txt         the platform team's form
      shared       <clip>.mp4       + <sop>.txt          one SOP object for the sweep

    `shared` is only used for the third case, which is what a 1:1 run can get away
    with: the unpaired 12-clip sweep in reference/sweep/ returned verdicts for 10 of
    12 clips with mismatched stems.
    """
    if shared is not None:
        return api.upload_file(path), shared
    v_name, s_name = pair_names(stem, args.sop, args.pair_sop,
                                os.path.basename(path))
    if args.pair_sop:
        video_id = api.upload_file(path)
        committed = os.path.join("sop", "paired", f"{stem}.txt")
        if os.path.exists(committed) and open(committed).read() == sop_text(steps):
            return video_id, api.upload_file(committed)
        return video_id, api.upload_text(sop_text(steps), s_name)
    return (api.upload_file(path, rename=v_name),
            api.upload_text(sop_text(steps), s_name))


def submit_all(args, bundle_id: str, sop_id: str | None, clips: list[str],
               steps: list[str]) -> None:
    """Submit every run before collecting any, then poll them in order.

    Sequential submission leaves the GPU unclaimed between clips, and dev is
    shared across orgs — a job you cannot see can take the slot, which is the most
    likely explanation for an 11-minute gap observed between `job.admitted` and a
    pod starting. Submitting up front holds the positions.

    It buys queue position, NOT parallelism: the runs still execute one at a time.
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
        video_id, clip_sop_id = upload_pair(args, path, stem, steps, sop_id)
        agent_id = api.run(bundle_id, video_id, clip_sop_id)
        print(f"[{i}/{len(clips)}] submitted {stem:<32s} {agent_id}")
        pending.append((stem, agent_id, out_path))

    print(f"\n{len(pending)} run(s) queued. They still execute ONE AT A TIME; "
          f"collecting in submission order.\n"
          f"Interrupting now leaves them running — cancel with:\n"
          f"  python3 sweep.py --cancel-all\n")
    log = []
    for stem, agent_id, out_path in pending:
        t0 = time.time()
        verdict = api.poll_logs(agent_id, quiet=True)
        payload = api.fetch_results(agent_id, out_path)
        n = 0
        if payload:
            recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines() if l.strip()]
            n = sum(len(r.get("results", [])) for r in recs)
        print(f"  {stem:<32s} {verdict} in {time.time() - t0:.0f}s — {n} step(s), "
              f"{len(payload or b'')} bytes")
        log.append({"clip": stem, "agent_id": agent_id, "verdict": verdict,
                    "steps": n})
    with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as fh:
        json.dump(log, fh, indent=2)
    print(f"\nscore it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


def collect(out_dir: str, bundle_id: str | None = None,
            limit: int = 100) -> None:
    """Download the output of every finished run, after the fact.

    The recovery path, and the reason it exists: the runs live on the PLATFORM,
    not in the client. A client that dies — closed laptop, lost network, Ctrl-C,
    an exhausted retry budget — abandons results that are already computed and
    that do not expire. This walks the org's instances and harvests whatever is
    ready, so a sweep never has to be re-run because nobody was listening.

    Safe to run repeatedly: it skips clips already on disk and ignores runs that
    are still going.
    """
    instances = api.request("GET",
                            f"{api.agents_base()}/instances?limit={limit}")
    rows = [x for x in instances.get("data", [])
            if x.get("blueprint") == "tva"
            and (bundle_id is None or x.get("bundle_id") == bundle_id)]
    if not rows:
        print("no tva runs found" + (f" for bundle {bundle_id}" if bundle_id else ""))
        return

    os.makedirs(out_dir, exist_ok=True)
    saved = skipped = pending = empty = dead = 0
    for x in sorted(rows, key=lambda r: str(r.get("created_at"))):
        aid, status = x["id"], x.get("status")
        # Ask for results regardless of `status`, which is not a reliable
        # terminality signal — a finished job has been seen still reading
        # `running`. An unfinished run simply returns nothing.
        res = api.request("GET", f"{api.agents_base()}/instances/{aid}/results")
        items = res.get("data") or []
        if not items:
            # "No results" means two completely different things, and conflating
            # them tells you to keep waiting for a run that is already over. A run
            # that logged an ERROR, or reached a terminal event, will never produce
            # output — one clip failed its S3 video download, logged an ERROR, and
            # still reported job.completed.
            rows = sorted(api.request(
                "GET", f"{api.agents_base()}/instances/{aid}/logs?limit=500"
            ).get("data", []), key=lambda r: str(r.get("created_at")))
            errors = [r for r in rows if str(r.get("level")) == "ERROR"]
            terminal = bool(rows) and str(rows[-1].get("event_type")) in api.TERMINAL_EVENTS
            if errors:
                dead += 1
                msg = str(errors[0].get("message"))
                # Lead with the CLIP, not the agent id: "which one failed?" is the
                # first question, and the name is otherwise buried mid-message
                # behind a long s3:// URI.
                m = re.search(r'"([^"]+\.(?:mp4|mkv|webm|mov|m4v|avi))"', msg)
                who = m.group(1) if m else "(clip not named in the error)"
                print(f"  FAILED  {who}  ({aid})")
                print(f"      it will not produce output. cause: {msg[:200]}")
            elif terminal:
                dead += 1
                print(f"  {aid}  FINISHED   no results — terminal "
                      f"({rows[-1].get('event_type')}) with nothing emitted")
            else:
                pending += 1
                print(f"  {aid}  {status:9}  still in flight, no results yet")
            continue
        inner = items[0].get("data") or {}
        # The output filename carries the clip: agt_<id>__output_<clip>.json
        name = str(inner.get("filename") or "")
        # The platform names outputs "<agent>__output_<clip>.json" — .json, not
        # a different extension than you expect. Strip whatever extension is
        # there rather than one specific suffix, or you get "<clip>.json.json".
        stem = os.path.splitext(name.split("__output_", 1)[-1])[0] or aid
        out_path = os.path.join(out_dir, f"tva-output-{stem}.json")
        if os.path.exists(out_path):
            skipped += 1
            continue
        payload = api.fetch_results(aid, out_path)
        if payload is None:
            pending += 1
            continue
        recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines()
                if l.strip()]
        n = sum(len(r.get("results", [])) for r in recs)
        if n == 0:
            empty += 1
        print(f"  {aid}  {status:9}  {stem:<32s} {n} verdict(s), "
              f"{len(payload)} bytes -> {out_path}")
        saved += 1

    print(f"\nsaved {saved}, already had {skipped}, still in flight {pending}"
          + (f", EMPTY {empty} (reasoning overflow — see the README)" if empty else "")
          + (f", FAILED {dead} (never will produce output)" if dead else ""))
    if saved or skipped:
        print(f"score it:  python3 evaluate.py {out_dir}/tva-output-*.json")


def cancel_all() -> None:
    """Cancel every non-terminal run in the org. The escape hatch for --submit-all."""
    instances = api.request("GET", f"{api.agents_base()}/instances?limit=100")
    live = [x for x in instances.get("data", [])
            if x.get("status") not in ("completed", "failed", "canceled")]
    if not live:
        print("no non-terminal runs")
        return
    for x in live:
        api.cancel(x["id"])
        print(f"cancelled {x['id']} ({x.get('status')})")


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
                         "Does not make the GPU faster — dev serializes — but it "
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
                         "SOP file. Cheapest — no per-clip video copies — but stems do "
                         "not match and the platform cannot later tell you which SOP a "
                         "run used. This was the old default and is how "
                         "reference/sweep/ was produced.")
    ap.add_argument("--pair-sop", action="store_true",
                    help="upload one SOP per clip named <clip-stem>.txt, from "
                         "sop/paired/ when present. The pipeline pairs video and "
                         "text by stem because inputs arrive as one flat list of "
                         "file ids, so a shared SOP name is only safe for 1:1 runs. "
                         "Also removes the risk of concurrent runs clobbering a "
                         "shared SOP object.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan — bundle values, clips, which are "
                         "already done, estimated time — and stop. No API calls.")
    ap.add_argument("--sop", metavar="FILE", default=DEFAULT_SOP_PATH,
                    help=f"SOP steps, one per line (default: {DEFAULT_SOP_PATH})")
    ap.add_argument("--collect", action="store_true",
                    help="download the output of every finished tva run and stop. "
                         "Use after a disconnect, a closed laptop or a Ctrl-C: the "
                         "runs continue on the platform and their results do not "
                         "expire, so nothing needs re-running. Safe to repeat.")
    ap.add_argument("--bundle", metavar="ID",
                    help="with --collect, restrict to one bundle's runs.")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel every non-terminal run in the org and stop. The "
                         "escape hatch after --submit-all, since an abandoned run "
                         "blocks the shared GPU.")
    args = ap.parse_args()

    # --fresh REPLACES outputs, and reference/ holds the committed evidence every
    # number in the README is scored from. Losing it costs 2.6 h of shared GPU to
    # regenerate, and the regenerated runs would not be the ones the README cites.
    if args.fresh and os.path.normpath(args.out_dir).split(os.sep)[0] == "reference":
        sys.exit(f"refusing --fresh into {args.out_dir}: reference/ is committed "
                 f"evidence, not a scratch directory.\n"
                 f"  use a new directory instead:  --out-dir eval_rerun")

    if args.collect:
        collect(args.out_dir, args.bundle)
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
        print(f"blueprint  {args.blueprint}")
        print(f"values     {json.dumps(values)}")
        # Show the ACTUAL ids for a representative clip, not a guess: the naming
        # differs per mode, and a --dry-run that prints the wrong name is worse than
        # one that prints nothing.
        sample = os.path.splitext(os.path.basename((todo or done or ["<clip>"])[0]))[0]
        if args.shared_sop:
            sample_v, sample_s = f"{sample}.mp4", os.path.basename(args.sop)
        else:
            sample_v, sample_s = pair_names(sample, args.sop, args.pair_sop)
        print(f"SOP        {args.sop} ({len(steps)} steps)")
        print(f"inputs     {sample_v} + {sample_s}"
              f"{'   <- stems match' if sample_v.rsplit('.',1)[0] == sample_s.rsplit('.',1)[0] else '   <- stems DIFFER'}")
        print(f"bundle     ONE, reused for all runs")
        print(f"outputs    {args.out_dir}/tva-output-<clip>.json")
        if done:
            print(f"\nalready done, would be SKIPPED ({len(done)}):")
            for stem in done:
                print(f"  {stem:<32s} {label_str(stem)}")
        # --submit-all changes WHEN runs are submitted, not how fast they execute:
        # dev serializes on one GPU either way. Say which mode, so the plan does not
        # read as "sequential" when the flag was passed, but do not imply parallelism.
        mode = ("all submitted up front, then collected in order — still ONE AT A "
                "TIME on the GPU" if args.submit_all else "submitted one at a time")
        print(f"\nwould run ({len(todo)}), {mode}, ~13 min each "
              f"-> ~{len(todo) * 13} min (~{len(todo) * 13 / 60:.1f} h):")
        for i, stem in enumerate(todo, 1):
            print(f"  {i:2}. {stem:<32s} {label_str(stem)}")
        return

    os.makedirs(args.out_dir, exist_ok=True)
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
    print(f"{len(clips)} clip(s), sequential, ~13 min each "
          f"-> ~{len(clips) * 13} min total, plus queue if the GPU is busy\n")

    # One upload for the whole sweep: every clip is checked against the SAME
    # procedure, so the SOP is uploaded once and its file_id reused.
    sop_id = None
    if args.shared_sop:
        # One SOP object for the whole sweep. Cheapest, and stems do NOT match — fine
        # for 1:1 runs (reference/sweep/ was made this way), but it means the platform
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

    # ONE bundle for the whole sweep, reused by every run.
    #
    # A bundle carries only `values`, and they are identical for every clip here.
    # The two things that vary per run — the video and the SOP — are both source
    # connector inputs, supplied at run time. So there is nothing a per-clip
    # bundle could hold that differs, and creating twelve identical bundles just
    # to launch twelve runs is noise in the console and twelve extra API calls.
    #
    # This is the same property the README's Stage 2 describes from the other
    # direction: because the SOP travels with the run rather than the bundle, one
    # bundle serves every SOP — and a fortiori every clip under one SOP.
    bundle = api.create_bundle(args.blueprint, f"TVA sweep ({len(clips)} clips)",
                               values)
    print(f"bundle {bundle['id']} (reused for all {len(clips)} runs)\n")
    if args.submit_all:
        submit_all(args, bundle["id"], sop_id, clips, steps)
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
        agent_id = None
        try:
            video_id, clip_sop_id = upload_pair(args, path, stem, steps, sop_id)
            agent_id = api.run(bundle["id"], video_id, clip_sop_id)
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
    print(f"score it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


if __name__ == "__main__":
    main()
