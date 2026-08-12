#!/usr/bin/env python3
"""Run the Task Verification Agent on one assembly video, end to end.

Single file, stdlib only apart from tva_api.py next to it — no pip install, no
virtualenv, no GPU, no atai_core checkout.

    ATAI_API_KEY=<dev key>
    ATAI_API_ENDPOINT=https://api.dev.u1.archetypeai.app

    python3 run_tva_example.py                      # the default clip
    python3 run_tva_example.py --video data/clips/1_pass_2_pass_3_fail_A.mp4
    python3 run_tva_example.py --dry-run            # print the payloads, call nothing
    python3 run_tva_example.py --show out.jsonl     # re-read an output, no API calls

    # reproduce the empty-output failure — the one worth recognising
    python3 run_tva_example.py --repro-empty --offline   # free, no API key
    python3 run_tva_example.py --repro-empty             # live, ~13 min

Expect ~13 minutes (754-814 s observed): ~7 min of model download and load on
EVERY run — nothing is cached between runs — then ~5 min of generation for a 30 s
clip. Dev has one GPU and serializes these, so a concurrent run queues behind
yours; 796 s of queue wait has been observed.

WHAT IS DIFFERENT FROM THE MGA EXAMPLE, and it is the whole point:

  TVA takes the reference procedure AT RUNTIME. The `tva` blueprint wires
  `source.text -> prepare.in` (PrepareSOPNode) alongside `source.video`, so a
  run sends TWO source inputs: the video and the SOP as a .txt file. MGA's
  blueprint sets `text_extensions: []` and hardcodes its instruction, which is
  why that example has no prompt to pass and this one does.

  MultiModalSource routes the two inputs by their `format` field, not by
  position: `mp4` reaches source.video, `txt` reaches source.text.

THINGS THAT WILL BITE YOU (all verified on dev, 2026-08-11)

  1. `connectors.source[].id` wants the file BASENAME, which is what /v0.5/files
     returns as `file_id` — not the `fil_...` `file_uid` in the same response.

  2. The bundle endpoint is PLURAL: POST /agents/bundles. The singular 404s.

  3. Starting a run returns 202, not 201.

  4. Poll /logs, not /events, and ignore the instance `status` field — it has
     reported `running` 20+ minutes after a pod died with exit=1.

  5. There is NO `prompt` value on this blueprint. The system message lives
     inside TaskVerificationPromptGeneratorNode. Your SOP is the only thing you
     control, so the SOP wording IS the prompt — see the README's Stage 3.

  6. These clips have NO AUDIO TRACK, and the blueprint runs
     audio_reader -> whisper -> prompt.audio regardless. It does not fail; see
     the README on what whisper contributes here (nothing).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

import tva_api as api

# The default clip: 31 s, 640x480, three steps all performed correctly. The
# filename encodes the ground truth — see parse_ground_truth() and the README.
CLIP_DIR = "data/clips"
VIDEO_PATH = "data/clips/1_pass_2_pass_3_pass_A.mp4"

# NORMALLY you target the blueprint KEY, not an id: a key resolves to whatever is
# canonical and active, and the MGA example had a pinned id stop resolving
# mid-session when the blueprint was republished under it.
#
# THIS FILE PINS AN ID ANYWAY, and the reason is the mirror image of that advice.
# `tva` was republished on 2026-08-11 at 22:01:18 with its sink format changed to
# `json/per-request`, which has no registered connector — so the CANONICAL, ACTIVE
# blueprint cannot produce output at all. It loads both models for ~7 minutes and
# then dies at graph instantiation (see tva_api.check_sink). The superseded
# blueprint below is byte-identical apart from that one string, and it works.
#
# So: neither "use the key" nor "pin an id" is safe on its own. What is safe is
# checking the blueprint document before spending GPU, which the preflight does.
#
# TO FLIP BACK once the sink is fixed upstream: set BLUEPRINT_DEFAULT = "tva".
# The preflight will stop warning on its own — it reads the document, not a
# hardcoded list of blueprint ids.
BLUEPRINT_ACTIVE = "tva"                                  # broken sink, see above
BLUEPRINT_WORKING = "blp_6dp7cye17f8qct7a9jbzx6yhfy"      # superseded, runs
BLUEPRINT_DEFAULT = BLUEPRINT_WORKING

# The SOP lives in a TEXT FILE, not in this script, because that is what it is on
# the wire: the runner writes it to a .txt, uploads it, and passes it as a second
# source input. Editing sop/oring.txt is the intended way to change the procedure
# — no Python involved — and --sop points at a different file.
#
# ONE STEP PER LINE. PrepareSOPNode "accumulates the SOP step lines into a single
# prepared-instruction string", so the LINE BREAKS are the step boundaries and
# nothing else is. A wrapped line becomes two steps; a blank line is dropped.
#
# Two deliberate choices in the shipped wording, both documented in the README:
#
#   * EXPLICIT "Step N:" PREFIXES. The line breaks alone are enough for the node
#     to segment on, so these are redundant to the parser — they are there for
#     the MODEL, to tie each verdict to a numbered step rather than to line
#     order. The parser's own `step` field is positional and 0-based regardless
#     (see STEP_BASE), so the prefixes cannot renumber the output.
#   * NAMED COLOURS. The bench holds a blue anodized cap and a black manifold,
#     with other blue and black fittings laid out beside them, so "the cap" is
#     genuinely ambiguous on a 224x224 frame. Worth knowing: the model already
#     volunteered "the blue cap" in its reasons WITHOUT being told the colour, so
#     this is disambiguation insurance rather than a fix for a known miss.
#
# The wording stays behavioural ("ensuring it is properly seated") and does not
# name a torque figure, because nothing in a 30-second handheld video can
# evidence a number — see the README on why step 3 is the one that misreads.
DEFAULT_SOP_PATH = "sop/oring.txt"


def load_sop(path: str = DEFAULT_SOP_PATH) -> list[str]:
    """Read SOP steps from a text file, one per line, blanks and # comments out."""
    if not os.path.exists(path):
        sys.exit(f"no SOP file at {path} — write one step per line, or pass --sop")
    steps = [l.strip() for l in open(path)
             if l.strip() and not l.lstrip().startswith("#")]
    if not steps:
        sys.exit(f"{path} has no step lines")
    return steps

# Statuses the TaskVerificationResultsParserNode emits, per the design doc.
STATUSES = ("PASSED", "FAILED", "MISSING")

# --repro-empty: a run that reports success and returns no verdicts at all.
# `job.completed`, no ERROR row, an empty `results` list.
#
# WHAT THIS REPRODUCES, and what it does not. Raising the budget is NOT a general
# fix — two clips returned empty at the ceiling in the 12-clip sweep — but a low
# budget is what reproduces it ON DEMAND, and a reproduction has to be reliable.
#
# The root pathology is REPETITION: the model emits the verdict block over and
# over. Whether that surfaces as nothing or as sevenfold duplication depends on
# which side of `</think>` the budget runs out on:
#
#   1_fail_2_pass_3_fail_A at 8192  -> looped inside <think>, never closed:
#                                      "dropping 37 rehearsed row(s)", 0 rows out
#   1_fail_2_pass_3_fail_A at 5760  -> closed <think>, then looped in the answer:
#                                      21 rows, all CORRECT, 7 blocks of 3
#   1_pass_2_pass_3_fail_A at 2048  -> runs out inside <think>: 0 rows  <- this mode
#
# So do not read a small budget as "the cause". It is the reliable trigger for the
# empty presentation of a pathology that also occurs at the ceiling.
REPRO = {
    "video": "data/clips/1_pass_2_pass_3_fail_A.mp4",
    "max_frames": 64,
    "size": 224,
    # 2048 rather than the ceiling, and this was learned the hard way. An earlier
    # version used the computed maximum on 1_fail_2_pass_3_fail_A, reasoning that
    # reproducing the failure at the ceiling was the honest configuration. It does
    # not reproduce: that clip at 5760 returns 21 rows of CORRECT verdicts. The
    # underlying pathology is repetition — the model emits the verdict block over
    # and over — and whether you get nothing or sevenfold depends on which side of
    # `</think>` the budget runs out on. 2048 lands inside the reasoning block
    # reliably on this clip, which is what a reproduction needs to do.
    "max_new_tokens": 2048,
}
# What that clip produced in the sweep, committed so the failure is inspectable
# offline with no API key.
REPRO_REFERENCE = "reference/tva-output-1_pass_2_pass_3_fail_A-mnt2048-EMPTY.jsonl"
REPRO_EXPECTED_MD5 = "1b56135df5d1476c625e6aedfd18b91b"

# The avoidable variant, kept only so the contrast is available:
#   python3 run_tva_example.py --repro-empty --max-new-tokens 2048
# on 1_pass_2_pass_3_fail_A. That one IS fixed by raising the budget; the default
# above is not, which is the point.


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

    Normally this uploads sop/oring.txt ITSELF — no generated copy, so the file
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

    So sop/oring.txt arrives as `oring.txt` and the local file and the platform
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
            print("  NO STEPS PARSED — the run reported job.completed and "
                  "returned an empty results list.\n"
                  "  The usual cause is the token budget being spent inside the "
                  "model's <think>\n"
                  "  reasoning block, so no verdicts were ever emitted. Check "
                  "/logs for\n"
                  "  'generation ended inside the model's reasoning block' and "
                  "raise --max-new-tokens.\n"
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

    The design doc specifies `step: <int>` without saying where it starts. It is
    0-BASED, verified on a real run: a 3-step SOP comes back as steps 0, 1, 2.
    That matters because the SOP a human writes is 1-based, so a scorer that
    joins them naively is off by one — and an off-by-one here produces plausible
    wrong alignments (step 2's verdict filed under step 1) rather than an error.
    """
    s = row.get("step")
    return None if s is None else int(s) + (1 - STEP_BASE)


def repro_symptoms(payload: bytes | None) -> list[tuple[str, bool, str]]:
    """(description, reproduced, detail) for each symptom of the empty failure."""
    records = [json.loads(l) for l in (payload or b"").decode(errors="replace").splitlines()
               if l.strip()]
    rows = [r for rec in records for r in (rec.get("results") or [])]
    return [
        ("a record IS returned (this is not a missing file)",
         len(records) == 1, f"{len(records)} record(s)"),
        ("it contains NO verdicts",
         len(rows) == 0, f"{len(rows)} verdict(s)"),
        ("the output is tiny (~44 bytes)",
         payload is not None and len(payload) < 100, f"{len(payload or b'')} bytes"),
    ]


def repro_report(payload: bytes | None, verdict: str, agent_id: str) -> None:
    """Print CONFIRMED / NOT REPRODUCED per symptom, and what it means."""
    print("\n" + "=" * 72)
    print("EMPTY-OUTPUT REPRODUCTION")
    print("=" * 72)
    results = repro_symptoms(payload)
    for desc, ok, detail in results:
        print(f"  {'CONFIRMED    ' if ok else 'NOT REPRODUCED'}  {desc}\n"
              f"                  observed: {detail}")
    # The verdict from the log stream matters as much as the bytes: the whole point
    # is that the job does not report failure.
    print(f"\n  run verdict from /logs: {verdict}")
    if verdict == "empty-reasoning-overflow":
        print("    -> the parser reported that generation ended inside the model's\n"
              "       reasoning block, so no verdict was ever emitted. THIS is the\n"
              "       diagnostic; the job itself reports completed.")
    elif verdict == "completed":
        print("    -> reported as a clean completion. Note there was no WARN about\n"
              "       the reasoning block, so if the output is empty this is the\n"
              "       OTHER empty-output mode: the parser matched nothing at all.")

    if all(ok for _, ok, _ in results):
        digest = hashlib.md5(payload).hexdigest()
        print(f"\n  REPRODUCED. md5 {digest}")
        print(f"    {'matches' if digest == REPRO_EXPECTED_MD5 else 'differs from'} "
              f"the committed reference ({REPRO_EXPECTED_MD5})")
        print("\n  Why it matters: nothing in the HTTP responses, the job status or\n"
              "  the exit code distinguishes this from success. COUNT THE VERDICTS.\n"
              "  And note which clips it strikes — the ones containing a defect need\n"
              "  more reasoning than the clean ones, so a verification agent fails\n"
              "  precisely on the inputs it was deployed to catch.")
    else:
        print("\n  NOT REPRODUCED — the run returned verdicts.\n"
              "  The budget needed depends on the clip and on how much the model\n"
              "  reasons, so this is not perfectly deterministic. Try\n"
              "  --repro-empty on a clip with more than one skipped step, or lower\n"
              "  --max-new-tokens further.")
    print(f"\n  full log: {api.agents_base()}/instances/{agent_id}/logs")
    print("=" * 72)


def repro_analyse(path: str) -> None:
    """Offline: report the same symptoms from the committed output. No API key."""
    if not os.path.exists(path):
        sys.exit(f"no committed reference at {path}")
    payload = open(path, "rb").read()
    print(f"analysing the committed output — no API key, no GPU\n  {path}\n")
    print(f"  raw bytes: {payload.decode(errors='replace')}\n")
    for desc, ok, detail in repro_symptoms(payload):
        print(f"  {'CONFIRMED    ' if ok else 'NOT REPRODUCED'}  {desc}\n"
              f"                  observed: {detail}")
    print(f"\n  md5 {hashlib.md5(payload).hexdigest()}")
    print("\n  This is what `job.completed` with no ERROR row can look like.\n"
          "  Reproduce it live with:  python3 run_tva_example.py --repro-empty")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--video", default=VIDEO_PATH)
    ap.add_argument("--blueprint", default=BLUEPRINT_DEFAULT,
                    help=f"blueprint id or KEY (default: {BLUEPRINT_DEFAULT!r}). "
                         f"A key tracks whatever is canonical and active; a pinned "
                         f"id can stop resolving when the blueprint is republished.")
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
                    help="output budget, capped by max_seq_len. MUST BE STRICTLY "
                         "BELOW max_seq_len, which is 8192 in this deployment and "
                         "is NOT exposed — 8192 is rejected at graph "
                         "instantiation. The prompt (video frames + SOP) and the "
                         "generated tokens SHARE that 8192, so at max_frames=64 "
                         "only ~6400 remain for the model's <think> block plus the "
                         "verdicts. Raising this cannot buy more than the ceiling "
                         "allows. (default: 8191, the maximum accepted)")
    ap.add_argument("--sop", metavar="FILE", default=DEFAULT_SOP_PATH,
                    help=f"text file of SOP steps, one per line "
                         f"(default: {DEFAULT_SOP_PATH}). The SOP is the ONLY "
                         f"thing you control on this blueprint — there is no "
                         f"`prompt` value — so this file is the whole lever.")
    ap.add_argument("--name", default="TVA o-ring cap assembly")
    ap.add_argument("--output", default=None,
                    help="where to save the JSONL (default: tva-output-<clip>.jsonl)")
    ap.add_argument("--show", metavar="JSONL",
                    help="pretty-print an output file and stop — no API calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact payloads and stop — no API calls")
    ap.add_argument("--repro-empty", action="store_true",
                    help="reproduce the EMPTY-OUTPUT failure: a run that reports "
                         "job.completed with no ERROR and returns no verdicts. "
                         "Overrides --video/--max-frames/--size/--max-new-tokens "
                         "with the configuration known to trigger it, then reports "
                         "each symptom as CONFIRMED or NOT REPRODUCED. ~13 min of "
                         "GPU. Add --offline to inspect the committed output "
                         "instead, for free.")
    ap.add_argument("--offline", action="store_true",
                    help="with --repro-empty, analyse the committed reference "
                         "output instead of running the agent. No API key needed.")
    ap.add_argument("--force", action="store_true",
                    help="run even if the blueprint's sink cannot be "
                         "instantiated (see check_sink) — normally that is a "
                         "guaranteed 7-minute failure, so it aborts instead.")
    args = ap.parse_args()

    if args.show:
        show(args.show)
        return

    if args.repro_empty:
        if args.offline:
            repro_analyse(REPRO_REFERENCE)
            return
        print(__doc__.split("REPRODUCING")[0].strip().splitlines()[0])
        args.video = REPRO["video"]
        args.max_frames = REPRO["max_frames"]
        args.size = REPRO["size"]
        # The repro's own budget wins unless the caller set one explicitly, so
        # --repro-empty is self-contained and --max-new-tokens still overrides it.
        if args.max_new_tokens is None:
            args.max_new_tokens = REPRO["max_new_tokens"] or \
                api.max_usable_new_tokens(args.max_frames, args.size,
                                          sop=args.sop)
        ceiling = api.max_usable_new_tokens(args.max_frames, args.size, sop=args.sop)
        at_ceiling = args.max_new_tokens >= ceiling
        print(f"\nREPRODUCING THE EMPTY-OUTPUT FAILURE\n"
              f"  clip            {os.path.basename(REPRO['video'])}\n"
              f"                  step 3 skipped — the wrench never appears\n"
              f"  max_new_tokens  {args.max_new_tokens}"
              f"{'   <-- the MAXIMUM the context window allows' if at_ceiling else '   (below the ceiling of %d)' % ceiling}\n"
              f"  max_frames      {args.max_frames}, size {args.size}\n"
              f"  SOP             {args.sop or DEFAULT_SOP_PATH} (unmodified)\n\n"
              f"  Expect: job.completed, NO ERROR row, and results: [] — ~44 bytes.\n")
        print(f"  A low budget is the RELIABLE TRIGGER, not the root cause. The\n"
              f"  underlying pathology is repetition: the model emits the verdict\n"
              f"  block over and over, and where the budget runs out decides what\n"
              f"  you see. Measured on 1_fail_2_pass_3_fail_A:\n"
              f"      at 8192  looped inside <think>, never closed -> 0 rows\n"
              f"               (log: 'dropping 37 rehearsed row(s)')\n"
              f"      at 5760  closed <think>, looped in the answer -> 21 rows,\n"
              f"               7 identical blocks of 3, all verdicts CORRECT\n"
              f"  So raising the budget is not a fix — it changes the presentation.\n"
              f"  The ceiling here is {ceiling}; this run uses "
              f"{args.max_new_tokens}.\n")

    steps = load_sop(args.sop or DEFAULT_SOP_PATH)

    clip = os.path.splitext(os.path.basename(args.video))[0]
    out_path = args.output or f"tva-output-{clip}.jsonl"
    # Default to the MAXIMUM the context window allows, computed from the actual
    # frames/size — not a guessed constant. See api.max_usable_new_tokens().
    if args.max_new_tokens is None:
        args.max_new_tokens = api.max_usable_new_tokens(args.max_frames, args.size, sop=args.sop)
        print(f"max_new_tokens not set -> using {args.max_new_tokens}, the most "
              f"that fits beside a ~{api.estimate_prompt_tokens(args.max_frames, args.size, args.sop)}-token "
              f"prompt in max_seq_len={api.MAX_SEQ_LEN}")
    values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens,
              "size": args.size}

    if args.dry_run:
        endpoint = api.api_base()
        print(f"POST {endpoint}/agents/bundles")
        print(json.dumps({"blueprint": args.blueprint, "name": args.name,
                          "values": values}, indent=2))
        print(f"\nPOST {endpoint}/agents/bundles/<id>/run")
        print(json.dumps({"connectors": {"source": [
            {"type": "file", "id": os.path.basename(args.video), "format": "mp4"},
            {"type": "file", "id": sop_upload_name(args.sop), "format": "txt"},
        ]}}, indent=2))
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
    # blueprint document, so it needs no maintenance when the blueprint changes.
    bp = api.get_blueprint(args.blueprint)
    doc = bp["document"]
    print(f"blueprint {bp['id']} (key={bp['blueprint_key']}, "
          f"active={bp['is_active']}, canonical={bp.get('is_canonical')})")
    if not bp["is_active"]:
        print("  NOTE: this blueprint is superseded — results cannot ship.")
    inert = api.inert_values(doc, values)
    if inert:
        print(f"  WARNING: {inert} is not wired on this blueprint and will be "
              f"silently ignored.")
    # The expensive one: a sink the platform cannot instantiate wastes the whole
    # cold start before failing. Catch it here, for free.
    budget_warning = api.check_token_budget(args.max_frames, args.max_new_tokens, args.size, sop=args.sop)
    if budget_warning:
        print(f"  ** {budget_warning}")
        if not args.force:
            sys.exit("refusing to start a run that cannot fit in the context "
                     "window. Pass --force to do it anyway.")
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
    video_id = api.upload_file(args.video)
    sop_id = upload_sop(args.sop, steps)
    print(f"  video file_id={video_id}\n  sop   file_id={sop_id}")

    # No `artifacts` map: the tva blueprint pins newton-fusion:1.0 and
    # whisper:large-v3 itself, same as mga and unlike red/osm.
    bundle = api.create_bundle(args.blueprint, args.name, values)
    print(f"creating bundle ...\n  bundle_id={bundle['id']}  "
          f"status={bundle.get('status')}")

    agent_id = api.run(bundle["id"], video_id, sop_id)
    print(f"started run\n  agent_id={agent_id}\n"
          f"  watching /logs (~7 min of model loading first) ...")

    verdict = api.poll_logs(agent_id)
    print(f"\nrun {verdict}")

    payload = api.fetch_results(agent_id, out_path)
    if payload is not None:
        print(f"  saved {out_path} ({len(payload)} bytes, "
              f"md5 {hashlib.md5(payload).hexdigest()})")
        show(out_path, video=args.video)
    if args.repro_empty:
        repro_report(payload, verdict, agent_id)
        return
    if verdict != "completed":
        sys.exit(f"run {verdict} — full log: "
                 f"{api.agents_base()}/instances/{agent_id}/logs")


if __name__ == "__main__":
    main()
