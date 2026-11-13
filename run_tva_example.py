#!/usr/bin/env python3
"""Run the Task Verification Agent on one assembly video, end to end.

Stdlib only apart from tva_api.py and tva_project.py next to it: no pip install,
no virtualenv, no GPU.

    ATAI_API_KEY=<API key>
    ATAI_API_ENDPOINT=https://platform.u1.archetypeai.app/api

    python3 run_tva_example.py                      # the default clip
    python3 run_tva_example.py --video data/clips/1_pass_2_pass_3_fail_A.mp4
    python3 run_tva_example.py --dry-run            # print the payloads, call nothing
    python3 run_tva_example.py --show out.json      # re-read an output, no API calls

A run is: the project's copy of `tva` at this run's max_frames, size and
max_new_tokens (made once per setting, see tva_project.py), its package and batch
deployment (also once per setting), and one batch session over the video and the
SOP. About 10-15 minutes, most of it loading the model on every run.
state/lifecycle.json keeps the ids; `python3 cleanup.py --yes` deletes them.

TVA takes the reference procedure AT RUNTIME: a run sends two source inputs, the
video (`format: mp4`) and the SOP as a .txt file (`format: txt`). The platform
routes them by `format`, not by position.

THINGS THAT WILL BITE YOU

  1. `connectors.source[].id` wants the file BASENAME, which is what the files API
     returns as `file_id`, not the `fil_...` `file_uid` in the same response.
  2. A package, a deployment and a batch session take NO values. They are set on
     the blueprint copy (`overrides.values`), one copy per setting.
  3. A session that ends `completed` can hold no verdicts (`results: []`). The
     script counts them and exits non-zero when there are none.
  4. There is NO `prompt` value on this blueprint: your SOP is the only thing you
     control, so the SOP wording is the prompt.
  5. These clips have no audio track. The blueprint transcribes audio anyway; it
     does not fail, and contributes nothing here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

import tva_api as api
import tva_project as project
from background import add_background_flag, maybe_detach

# The default clip: 31 s, 640x480, three steps all performed correctly. The
# filename encodes the ground truth — see parse_ground_truth() and the README.
CLIP_DIR = "data/clips"
VIDEO_PATH = "data/clips/1_pass_2_pass_3_pass_A.mp4"

# Copy from the blueprint KEY, which resolves to the current version of `tva`.
BLUEPRINT_DEFAULT = "tva"

# The SOP lives in a text file, one step per line: the line breaks are the step
# boundaries. "Step N:" prefixes tie each verdict to a numbered step for the model;
# the parser's own `step` field is positional and 0-based regardless (STEP_BASE).
# Named colours ("the blue cap") disambiguate parts on a 224x224 frame.
DEFAULT_SOP_PATH = "sop/oring-numbered.txt"

def load_sop(path: str = DEFAULT_SOP_PATH) -> list[str]:
    """Read SOP steps from a text file, one per line, blanks and # comments out."""
    if not os.path.exists(path):
        sys.exit(f"no SOP file at {path} — write one step per line, or pass --sop")
    steps = [l.strip() for l in open(path)
             if l.strip() and not l.lstrip().startswith("#")]
    if not steps:
        sys.exit(f"{path} has no step lines")
    return steps

# The statuses the results parser emits.
STATUSES = ("PASSED", "FAILED", "MISSING")

# Extensions a clip may carry. `ls data/clips` shows names WITH the extension, so
# pasting one is the natural thing to do — accept both that and the bare stem
# that --list prints.
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")


def resolve_clip(name: str, clip_dir: str) -> str | None:
    """Resolve a user-supplied clip to a path, or None.

    Accepts, in order: an existing path; a name already carrying a video
    extension, inside clip_dir; and a bare stem, to which each known extension is
    appended. An earlier version appended ".mp4" unconditionally, so passing the
    filename that `ls` shows produced "<name>.mp4.mp4" and a confusing "no such
    clip" for a clip that was plainly right there.
    """
    if os.path.exists(name):
        return name
    candidates = [os.path.join(clip_dir, name)] if name.lower().endswith(VIDEO_EXTS) \
        else [os.path.join(clip_dir, name + ext) for ext in VIDEO_EXTS]
    return next((c for c in candidates if os.path.exists(c)), None)


def sop_text(steps: list[str]) -> str:
    return "\n".join(s.strip() for s in steps) + "\n"


def upload_sop(sop_path: str, steps: list[str]) -> str:
    """Upload the SOP and return its file_id (which is its basename).

    Normally this uploads sop/oring-numbered.txt ITSELF — no generated copy, so the file
    on disk, the file on the platform and the text the model sees are the same
    bytes under the same name.

    load_sop() drops blank lines and # comments, so a file using either would
    differ from what the model should see. Only then is a normalized copy written
    (to data/) and uploaded instead, and it says so rather than doing it quietly.
    """
    normalized = sop_text(steps)
    if open(sop_path).read() == normalized:
        return api.upload_file(sop_path)
    print(f"  note: {sop_path} has blank/comment lines; uploading a normalized "
          f"copy of its {len(steps)} step lines")
    return api.upload_text(normalized, sop_upload_name(sop_path))


def sop_upload_name(sop_path: str = DEFAULT_SOP_PATH) -> str:
    """The name the SOP is uploaded under — just its basename.

    So sop/oring-numbered.txt arrives as `oring-numbered.txt` and the local file and the platform
    file have the same name. Earlier revisions of this script named the upload
    after the CLIP, which wrongly implied a per-clip procedure (all twelve clips
    share one SOP), and then after a content hash, which was unreadable.

    The trade-off of a stable name: the platform's `file_id` IS the basename, so
    editing the SOP and re-running REPLACES the copy on the platform, and a past
    run's exact text is no longer retrievable from there. That is fine — the SOP
    is committed in git, which is the actual system of record, and a changed SOP
    changes the output digest anyway.
    """
    return os.path.basename(sop_path)


def pair_names(clip: str, sop_path: str, pair_sop: bool,
               video_name: str | None = None,
               suffix: str | None = None) -> tuple[str, str]:
    """The file ids the video and SOP are uploaded under: (video, sop).

    ONE definition, used by both --dry-run and the real upload. They were computed
    separately once and immediately diverged — --dry-run kept printing the previous
    naming scheme after the default changed, which is the worst possible bug in a
    flag whose entire job is to tell you what a run would do.

    `suffix` is api.run_suffix() — `<UTC timestamp>-<4 hex>`, computed once per run.
    It makes the ids unique across CONCURRENT USERS: an org shares one flat file
    namespace and `file_id` is the basename, so two people running this example at
    once would otherwise write to the same names and destroy each other's queued
    runs. Both halves of the pair get the same suffix, because the pipeline matches
    them by stem.
    """
    tail = f"-{suffix}" if suffix else ""
    if pair_sop:
        # Clip stem only, plus the
        # uniqueness suffix — which preserves stem matching, the point of the flag.
        return f"{clip}{tail}.mp4", f"{clip}{tail}.txt"
    # Default: stems match AND record which SOP produced the run.
    stem = f"{clip}-{os.path.splitext(os.path.basename(sop_path))[0]}{tail}"
    return f"{stem}.mp4", f"{stem}.txt"


def parse_ground_truth(filename: str) -> dict[int, bool]:
    """Ground truth from a filename like 1_pass_2_pass_3_fail_A.mp4.

    Returns {step_number: was_performed_correctly}, 1-based to match the SOP.
    The trailing letter is just a take id (A, B, C...) and carries no label.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    return {int(m.group(1)): m.group(2).lower() == "pass"
            for m in re.finditer(r"(\d+)_(pass|fail)", stem, re.IGNORECASE)}


def show(path: str, video: str | None = None) -> None:
    """Pretty-print an output file, and score it if the filename carries labels."""
    raw = open(path, "rb").read()
    digest = hashlib.md5(raw).hexdigest()
    records = [json.loads(l) for l in raw.decode(errors="replace").splitlines()
               if l.strip()]
    if not records:
        print(f"{path} is empty — a run can complete and return nothing. "
              f"Count the steps; the status will not tell you.")
        return

    for rec in records:
        results = rec.get("results", [])
        rid = rec.get("id", "?")
        # The model sometimes LOOPS, re-emitting the whole verdict block, and the
        # parser keeps every match — 21 rows for a 3-step SOP has been observed.
        # Dumping all of them buries the answer, so collapse for display and say
        # what was collapsed. Placeholder rows (reason "...", no interval) are the
        # skeleton block and are not verdicts; drop them if a real row exists.
        distinct, order = {}, []
        for r in results:
            key = r.get("step")
            placeholder = str(r.get("reason") or "").strip() in ("", "...", "\u2026")
            prev = distinct.get(key)
            if prev is None:
                distinct[key] = r
                order.append(key)
            elif placeholder:
                continue
            elif str(prev.get("reason") or "").strip() in ("", "...", "\u2026"):
                distinct[key] = r          # a real row beats a placeholder
        shown = [distinct[k] for k in order]
        print(f"\nverification report for {rid} — {len(shown)} step(s)")
        if len(results) > len(shown):
            n_ph = sum(1 for r in results
                       if str(r.get("reason") or "").strip() in ("", "...", "\u2026"))
            print(f"  NOTE: the model emitted {len(results)} rows for "
                  f"{len(shown)} step(s) — it looped, and the parser kept every\n"
                  f"  repetition ({len(results) // max(1, len(shown))} blocks"
                  + (f", {n_ph} placeholder row(s) with no reason" if n_ph else "")
                  + "). Collapsed below; the\n"
                  f"  duplicates were identical. See the README on repetition.")
        results = shown
        if not results:
            print("  NO STEPS PARSED — the session ended completed and "
                  "returned an empty results list.\n"
                  "  The usual cause is the token budget being spent inside the "
                  "model's <think>\n"
                  "  reasoning block, so no verdicts were ever emitted. Try "
                  "--max-new-tokens\n"
                  "  5760, the budget that returned verdicts here (see the "
                  "README).\n"
                  "  Clips where a step was SKIPPED need a bigger budget than "
                  "clean ones.")
        for r in results:
            # MISSING rows carry no timestamps, by design — there is no interval
            # to point at when the step never happened.
            ts, te = r.get("timestamp_start"), r.get("timestamp_end")
            span = f"[{ts:>5.1f} - {te:>5.1f}s]" if ts is not None and te is not None \
                else "[  no interval  ]"
            print(f"  step {_step_no(r)} (parser step={r.get('step')})  "
                  f"{str(r.get('status')):8} {span}  {r.get('reason', '')}")

        truth = parse_ground_truth(video or rid)
        if truth:
            print(f"\n  scored against the filename's labels "
                  f"({'/'.join('%d=%s' % (k, 'pass' if v else 'fail') for k, v in sorted(truth.items()))}):")
            for step, expected in sorted(truth.items()):
                got = next((r for r in results if _step_no(r) == step), None)
                status = got.get("status") if got else "NOT REPORTED"
                # PASSED is the only status that means "done correctly". FAILED
                # and MISSING are different observations but the same verdict.
                # A step with no row is a missing answer, not a correct FAIL —
                # see evaluate.py. Never scored as correct.
                predicted_pass = status == "PASSED"
                ok = "OK  " if got is not None and predicted_pass == expected \
                    else "MISS"
                print(f"    step {step}: expected "
                      f"{'PASS' if expected else 'FAIL':4}  got {str(status):8} "
                      f"[{ok}]")

    print(f"\nmd5 {digest}  ({len(raw)} bytes)")


# Verified against a real run: TaskVerificationResultsParserNode emits 0-based
# step numbers, the same as MGA's parser. Do not "fix" this to 1 without
# re-checking an actual output file.
STEP_BASE = 0


def _step_no(row: dict) -> int | None:
    """The parser's `step` field, normalised to 1-based to match the SOP.

    It is 0-BASED, verified on a real run: a 3-step SOP comes back as steps 0, 1, 2.
    That matters because the SOP a human writes is 1-based, so a scorer that
    joins them naively is off by one — and an off-by-one here produces plausible
    wrong alignments (step 2's verdict filed under step 1) rather than an error.
    """
    s = row.get("step")
    return None if s is None else int(s) + (1 - STEP_BASE)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--video", default=VIDEO_PATH)
    ap.add_argument("--blueprint", default=BLUEPRINT_DEFAULT,
                    help=f"the blueprint to copy into the project, by id or KEY "
                         f"(default: {BLUEPRINT_DEFAULT!r}). A key copies whatever "
                         f"is canonical and active; an id copies that version.")
    ap.add_argument("--max-frames", type=int, default=64,
                    help="frames uniformly sampled across the whole video. The "
                         "blueprint default is 16, which on a 30 s clip is one "
                         "frame every 1.9 s. 64 is also the reader and "
                         "preprocessor batch size, so they land in one batch. "
                         "(default: 64)")
    ap.add_argument("--size", type=int, default=224,
                    help="each frame is resized to size x size (blueprint default "
                         "224). Raising it recovers spatial detail — a ~10 mm "
                         "o-ring is a handful of pixels at 224 — but visual tokens "
                         "grow as (size/32)^2 PER temporal step and share the 8192 "
                         "context, so more size forces fewer frames. Source "
                         "resolution (null) is unreachable: 1080p costs ~3600 "
                         "tokens per step.")
    ap.add_argument("--max-new-tokens", type=int, default=None,
                    help="output budget. The prompt (video frames + SOP) and the "
                         "generated tokens SHARE the context window, so this is "
                         "capped by the model's context length. BIGGER IS NOT BETTER: the failure "
                         "mode is repetition, and on the double-omission clip 8192 "
                         "returned 0 rows where 5760 returned correct verdicts. "
                         f"(default: {api.DEFAULT_MAX_NEW_TOKENS}, or the window's "
                         "ceiling if that is lower)")
    ap.add_argument("--pair-sop", action="store_true",
                    help="upload the SOP as <video-stem>.txt, the CLIP's stem only. Not "
                         "the default, because one name per clip means every SOP "
                         "variant overwrites the last and a run in flight can be "
                         "changed under it. The default already matches stems AND "
                         "names the SOP (<clip>-<sop>.txt), so use this flag only to "
                         "use the plain convention.")
    ap.add_argument("--sop", metavar="FILE", default=DEFAULT_SOP_PATH,
                    help=f"text file of SOP steps, one per line "
                         f"(default: {DEFAULT_SOP_PATH}). The SOP is the ONLY "
                         f"thing you control on this blueprint — there is no "
                         f"`prompt` value — so this file is the whole lever.")
    ap.add_argument("--output", default=None,
                    help="where to save the result (default: tva-output-<clip>.json). "
                         "The tva sink is json/per-request, so the payload is JSON.")
    ap.add_argument("--show", metavar="FILE",
                    help="pretty-print an output file and stop — no API calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact payloads and stop — no API calls")
    ap.add_argument("--force", action="store_true",
                    help="run even if the blueprint's sink cannot be "
                         "instantiated (see check_sink) — normally that is a "
                         "guaranteed 7-minute failure, so it aborts instead.")
    add_background_flag(ap, default_log="logs/run_tva_example.log")
    args = ap.parse_args()
    maybe_detach(args)

    if args.show:
        show(args.show)
        return

    steps = load_sop(args.sop or DEFAULT_SOP_PATH)

    clip = os.path.splitext(os.path.basename(args.video))[0]
    out_path = args.output or f"tva-output-{clip}.json"
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
        endpoint = api.api_base()
        key = project.setting_key(values, args.blueprint)
        print("once per setting (reused while state/lifecycle.json holds it):\n")
        print(f"POST {endpoint}/agents/blueprints/{args.blueprint}/copy")
        print(json.dumps({"project_id": f"<{project.PROJECT}>", "blueprint_key": key,
                          "overrides": {"values": values}}, indent=2))
        print(f"POST {endpoint}/agents/packages")
        print(json.dumps({"blueprint_ids": ["<the copy>"], "target": {"kind": "platform"}},
                         indent=2))
        print(f"POST {endpoint}/agents/deployments")
        print(json.dumps({"package_id": "<the package>", "kind": "batch"}, indent=2))
        print(f"\nper run:\n\nPOST {endpoint}/agents/sessions")
        v_name, s_name = pair_names(clip, args.sop, args.pair_sop,
                                    os.path.basename(args.video)
                                    if os.path.splitext(args.video)[1] else None,
                                    suffix=api.run_suffix())
        print(json.dumps({"deployment_id": "<the deployment>", "connectors": {"source": [
            {"type": "file", "id": v_name, "format": "mp4"},
            {"type": "file", "id": s_name, "format": "txt"},
        ]}}, indent=2))
        print(f"\n  the -<UTC>-<hex> tail is generated per run, so the REAL run will "
              f"differ.\n  It keeps concurrent users from overwriting each other's "
              f"inputs.")
        print(f"\nthe SOP that would be uploaded ({len(steps)} steps, "
              f"one per line):\n")
        print(sop_text(steps))
        return

    video = resolve_clip(args.video, CLIP_DIR)
    if video is None:
        sys.exit(f"no such video: {args.video}\n"
                 f"  looked for it as a path, and in {CLIP_DIR}/ with and without "
                 f"a video extension.\n"
                 f"  run 'python3 sweep.py --list' to see what is there.")
    args.video = video

    # Preflight BEFORE uploading. A value the blueprint silently ignores should
    # be reported before spending 9 minutes of GPU, not after. This reads the
    # document of the blueprint the copy is made from, so it needs no maintenance
    # when the blueprint changes.
    bp = api.get_blueprint(args.blueprint)
    doc = bp["document"]
    print(f"blueprint {bp['id']} (key={bp['blueprint_key']}, "
          f"active={bp['is_active']}, canonical={bp.get('is_canonical')})")
    if not bp["is_active"]:
        print("  NOTE: this blueprint is not the current version.")
    inert = api.inert_values(doc, values)
    if inert:
        print(f"  WARNING: {inert} is not wired on this blueprint and will be "
              f"silently ignored.")
    # The expensive one: a sink the platform cannot instantiate wastes the whole
    # cold start before failing. Catch it here, for free.
    budget_warning = api.check_token_budget(args.max_frames, args.max_new_tokens, args.size, sop=args.sop)
    if budget_warning:
        print(f"  ** {budget_warning}")
    sink_warning = api.check_sink(doc)
    if sink_warning:
        print(f"  ** {sink_warning}")
        if not args.force:
            sys.exit("refusing to spend ~7 min of GPU on a run that cannot "
                     "produce output. Pass --force to do it anyway.")
    for k, v in sorted(values.items()):
        print(f"  {k:16} {str(v):8} -> "
              f"{'NOT WIRED — ignored' if k in inert else 'honoured'}"
              f"   (blueprint default: {doc.get('values', {}).get(k)})")
    # Worth stating plainly: unlike MGA there is no `prompt` here at all, so the
    # SOP is the entire lever. Show it, because it is what you are testing.
    print(f"\n  SOP in force ({len(steps)} steps, uploaded as a .txt input):")
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s}")

    print(f"\nuploading {args.video} "
          f"({os.path.getsize(args.video) / 1e6:.1f} MB) ...")
    # ONE suffix for this run, shared by both inputs.
    suffix = api.run_suffix()
    v_name, s_name = pair_names(clip, args.sop, args.pair_sop,
                                os.path.basename(args.video), suffix=suffix)
    if args.pair_sop:
        # The SOP named after the CLIP. Not the default: one name per clip means every
        # SOP variant overwrites the last, and content is resolved at RUN time, so a
        # second run can change a first run's SOP mid-flight.
        video_id = api.upload_file(args.video)
        paired = os.path.join("sop", "paired", f"{clip}.txt")
        if os.path.exists(paired) and open(paired).read() == sop_text(steps):
            sop_id = api.upload_file(paired)          # ship the committed file itself
        else:
            sop_id = api.upload_text(sop_text(steps), s_name)
    else:
        # DEFAULT: the stems match each other AND record which SOP produced the run.
        # The cost is one video copy per SOP variant; upload_file() is idempotent on
        # name+bytes, so it is paid once per variant, not per run.
        video_id = api.upload_file(args.video, rename=v_name)
        sop_id = api.upload_text(sop_text(steps), s_name)
    print(f"  video file_id={video_id}\n  sop   file_id={sop_id}")
    if video_id.rsplit(".", 1)[0] == sop_id.rsplit(".", 1)[0]:
        print(f"  stems match — nothing else writes to this pair")

    # The setting's copy, package and batch deployment: made on the first run at
    # this setting, reused after. No model is named: the tva blueprint pins
    # newton-fusion and whisper itself, same as mga and unlike red/osm.
    print(f"\nthe agent for {project.setting_key(values, args.blueprint)} ...")
    rec = project.agent(values, "batch", source=args.blueprint)
    print(f"  copy {rec['blueprint']}  package {rec['package']}  "
          f"deployment {rec['batch']}")

    session_id = api.open_session(rec["batch"], video_id, sop_id)["id"]
    project.record_session(session_id, setting=rec["key"], kind="batch", clip=clip,
                           out=out_path)
    print(f"started session\n  session_id={session_id}\n"
          f"  watching its status and /events (~7 min of model loading first) ...")

    try:
        verdict = api.poll_session(session_id)
    except KeyboardInterrupt:
        # Never leave a run on the GPU: an abandoned run blocks everyone behind it.
        print(f"\ninterrupted — cancelling {session_id}")
        api.cancel_session(session_id)
        sys.exit(130)
    print(f"\nsession {verdict}")

    payload = api.fetch_results(session_id, out_path)
    if payload is not None:
        print(f"  saved {out_path} ({len(payload)} bytes, "
              f"md5 {hashlib.md5(payload).hexdigest()})")
        show(out_path, video=args.video)
    if verdict != "completed":
        sys.exit(f"session {verdict} — {api.sessions_url(session_id)}/events")
    if api.empty_output(payload):
        sys.exit("session completed with results: [] — no verdicts (see the README "
                 "on max_new_tokens)")


if __name__ == "__main__":
    main()
