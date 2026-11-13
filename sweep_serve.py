#!/usr/bin/env python3
"""sweep.py over agent serving: load TVA once, then one /query per clip.

    python3 sweep_serve.py --all                       # every clip, one session
    python3 sweep_serve.py --clips 1_pass_2_pass_3_pass_A --repeat 3
    python3 sweep_serve.py --all --keep                # leave the session loaded
    python3 sweep_serve.py --all --session ses_…       # reuse it: no model load
    python3 evaluate.py eval_serving/tva-output-*.json

    python3 sweep_serve.py --all --chunk 4 --dry-run   # cut, print the plan, call nothing
    python3 sweep_serve.py --all --chunk 10 --overlap 4
    python3 merge_chunks.py eval_serving         # merge the chunks per rule, score each

With --chunk, each clip goes as several shorter videos instead of one, each
verified against the whole SOP, and every chunk's raw answer is saved to
<out-dir>/chunks/<clip>.json for merge_chunks.py. A chunk of 4 s or less fits
the cap as the clip's own bytes; longer ones are re-encoded, more lightly than
a whole clip.

Batch (sweep.py) pays ~7-12 min of model download and load on EVERY clip.
Serving deploys the setting's package (the same copy of `tva` sweep.py uses, see
tva_project.py) as `serving` and opens one session on it: the model loads once
and the session answers POST /agents/sessions/{id}/query for each clip, so
twelve clips cost one load plus twelve answers instead of twelve loads.

Limits:
  - the video goes inline (`"file_ids": ["base64:<mp4>"]`) and the whole body is
    capped at 2 MiB, so every clip is re-encoded to fit, into data/clips-serving/.
    TVA samples 64 frames at 224 px, so 640x480 at CRF 28 loses little, but the
    bytes differ from batch's and verdicts can too
  - each query has a 58 s budget; a 504 is retried
  - a serving session with no query for 30 minutes is closed (the deployment's
    default idle timeout)

The session is cancelled and the serving deployment stopped and deleted at the
end, Ctrl-C included, unless --keep: it holds a GPU worker.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import subprocess
import sys
import time

import tva_api as api
import tva_project as project
from background import add_background_flag, maybe_detach
from run_tva_example import DEFAULT_SOP_PATH, load_sop, resolve_clip, sop_text
from sweep import label_str

OUT_DIR = "eval_serving"
CLIP_CACHE = "data/clips-serving"
BODY_CAP = 2 * 1024 * 1024
# A chunk with fewer frames than the blueprint's max_frames ceiling (64): a 40-frame
# one took 10x longer to answer.
MIN_CHUNK_FRAMES = 64
# Tried in order until the request fits: full resolution first, since frames are
# what the model sees.
ENCODES = [("640:480", 28), ("640:480", 32), ("640:480", 36), ("448:336", 32), ("320:240", 32)]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def query_body(video: bytes, sop: str) -> dict:
    return {"model": "tv", "query": sop, "file_ids": ["base64:" + base64.b64encode(video).decode()]}


def body_size(video: bytes, sop: str) -> int:
    return len(json.dumps(query_body(video, sop)).encode())


def shrink(path: str, sop: str) -> str:
    """Re-encode a clip until its query fits the 2 MiB cap. Cached by name."""
    stem = os.path.splitext(os.path.basename(path))[0]
    with open(path, "rb") as f:
        if body_size(f.read(), sop) <= BODY_CAP:
            return path
    os.makedirs(CLIP_CACHE, exist_ok=True)
    for scale, crf in ENCODES:
        out = os.path.join(CLIP_CACHE, f"{stem}.mp4")
        cached = os.path.exists(out)
        if not cached:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"scale={scale}",
                            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow", "-an",
                            "-movflags", "+faststart", out], check=True)
        with open(out, "rb") as f:
            if body_size(f.read(), sop) <= BODY_CAP:
                return out
        os.remove(out)
        if cached:
            continue
    sys.exit(f"{path}: no encode fits the 2 MiB request cap")


def probe(path: str) -> tuple[float, float, list[float]]:
    """Duration, frame rate and keyframe times of a clip."""
    def ffprobe(*a: str) -> str:
        return subprocess.run(["ffprobe", "-v", "error", *a, "-of", "csv=p=0", path],
                              check=True, capture_output=True, text=True).stdout
    duration = float(ffprobe("-show_entries", "format=duration"))
    num, den = ffprobe("-select_streams", "v", "-show_entries", "stream=r_frame_rate").strip().split("/")
    keys = [float(t) for t in ffprobe("-select_streams", "v", "-skip_frame", "nokey",
                                      "-show_entries", "frame=pts_time").replace(",", " ").split()]
    return duration, float(num) / float(den), keys


def cut_chunks(path: str, seconds: float, overlap: float, sop: str) -> dict:
    """Cut a clip into chunks that each fit the request cap. Cached per clip and size.

    Chunks start on keyframes, so a chunk that fits is the clip's own bytes
    (`-c copy`, no re-encode); one that does not is re-encoded, lightest first.
    A chunk's verdict timestamps are relative to its start, so `offset` is kept.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    folder = os.path.join(CLIP_CACHE, stem, f"c{seconds:g}o{overlap:g}")
    manifest = os.path.join(folder, "manifest.json")
    if os.path.exists(manifest):
        with open(manifest) as f:
            return json.load(f)
    duration, fps, keys = probe(path)
    # Each chunk is meant to cover [t, t + seconds). It starts at the keyframe at or
    # before t, so it can start early, but it always runs to t + seconds: ending
    # `seconds` after an early start would leave a gap before the next chunk.
    windows, t = [], 0.0
    while True:
        start = max(k for k in keys if k <= t + 0.05)
        end = min(t + seconds + 0.05, duration)     # the next start may be up to 0.05 s late
        if windows and start <= windows[-1][0]:
            windows[-1] = (windows[-1][0], end)      # no keyframe since the last start
        else:
            windows.append((start, end))
        if t + seconds >= duration - 0.05:
            break
        t += seconds - overlap
    if len(windows) > 1 and (windows[-1][1] - windows[-1][0]) * fps < MIN_CHUNK_FRAMES + 2:
        windows[-2:] = [(windows[-2][0], windows[-1][1])]   # fold a short tail into the chunk before
    ladder = [("copy", ["-c:v", "copy"])] + [
        (f"{scale} crf{crf}", ["-vf", f"scale={scale}", "-c:v", "libx264", "-crf", str(crf), "-preset", "slow"])
        for scale, crf in [("640:480", 23)] + ENCODES]
    os.makedirs(folder, exist_ok=True)
    chunks = []
    for i, (start, end) in enumerate(windows):
        out = os.path.join(folder, f"{i:02d}.mp4")
        length = end - start
        for how, opts in ladder:
            # Seek a hair past the keyframe: rounded down, -ss lands before it and a
            # copy then starts at the keyframe before that one.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start + 0.001:.3f}", "-i", path,
                            "-t", f"{length:.3f}", *opts, "-an", "-avoid_negative_ts", "make_zero",
                            "-movflags", "+faststart", out], check=True)
            with open(out, "rb") as f:
                size = len(f.read())
            if body_size(b"\0" * size, sop) <= BODY_CAP:
                break
        else:
            sys.exit(f"{path} chunk {i}: no encode fits the 2 MiB request cap")
        chunks.append({"file": out, "offset": round(start, 3), "duration": round(length, 3),
                       "encode": how, "bytes": size})
    plan = {"source": path, "fps": fps, "duration": duration, "seconds": seconds,
            "overlap": overlap, "chunks": chunks}
    with open(manifest, "w") as f:
        json.dump(plan, f, indent=2)
    return plan


def verdict_doc(answer: dict, stem: str) -> dict | None:
    """A served answer as batch's {"id", "results"} document, so evaluate.py reads it.

    The served sink answers in atai-records: the step rows are `response.data`
    themselves, with no document around them.
    """
    rows = (answer.get("response") or {}).get("data")
    return {"id": stem, "results": rows} if isinstance(rows, list) else None


def new_events(session_id: str, seen: set) -> None:
    """Print the session's events not seen yet. A session has no /logs."""
    status, body = api.request("GET", f"{api.sessions_url(session_id)}/events?limit=100",
                               allow_status=True)
    if status != 200 or not isinstance(body, dict):
        return
    for row in reversed(body.get("data") or []):          # newest first
        key = (row.get("created_at"), row.get("message"))
        if key not in seen:
            seen.add(key)
            log(f"  {str(row.get('level', 'info')):7} {row.get('message')}")


def release(session_id: str, deployment_id: str | None, setting: str | None) -> str:
    """Cancel and delete the session, then stop and delete its serving deployment.

    The deployment is what holds the GPU worker; a session alone does not. The
    package and the copy stay: sweep.py's batch deployment uses them too, and
    cleanup.py deletes the package."""
    api.end_session(session_id)
    if not deployment_id:
        return f"cancelled and deleted {session_id}"
    api.end_deployment(deployment_id)
    if setting:
        project.forget(setting, "serving")
    return f"cancelled and deleted {session_id}, stopped and deleted {deployment_id}"


class AgentGone(Exception):
    """The session no longer answers: cancelled, or closed after 30 idle minutes."""


QUERY_TIMEOUT = 90      # the platform's budget is 58 s; past this the request is lost
QUERY_RETRIES = 3


def ask(session: str, body: dict, deadline: float, seen: set) -> tuple[dict, float, int]:
    """POST /query, retrying 5xx and timeouts (the cold-agent 504).

    Until `deadline` (the model load) it retries without limit; after that, a few
    times. A query that hangs never reaches the agent, and a session without a
    query for 30 minutes is closed, so a stuck request must not be waited out.
    """
    tries = 0
    while True:
        tries += 1
        t = time.time()
        code, answer = api.request("POST", f"{api.sessions_url(session)}/query",
                                   body=body, allow_status=True, retries=0, timeout=QUERY_TIMEOUT)
        took = time.time() - t
        if code == 200:
            return answer, took, tries
        new_events(session, seen)
        log(f"    {code or 'no answer'} after {took:.0f} s: {str(answer)[:160]}")
        if code == 409:
            raise AgentGone(str(answer))
        if not (code >= 500 or code == 0) or (tries > QUERY_RETRIES and time.time() > deadline):
            raise RuntimeError(f"query failed ({code}): {str(answer)[:300]}")
        time.sleep(15)


def resume(path: str, plan: dict) -> None:
    """Carry an earlier run's answers into `plan`, chunk by chunk."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        saved = {(c["offset"], c["duration"]): c.get("rows") for c in json.load(f)["chunks"]}
    for c in plan["chunks"]:
        if saved.get((c["offset"], c["duration"])) is not None:
            c["rows"] = saved[(c["offset"], c["duration"])]


def output_path(args, stem: str) -> str:
    if args.chunk:
        return os.path.join(args.out_dir, "chunks", f"{stem}.json")
    return os.path.join(args.out_dir, f"tva-output-{stem}.json")


def query_video(session: str, video_path: str, sop: str, stem: str, repeat: int,
                deadline: float, seen: set, entry: dict) -> dict | None:
    """Ask `repeat` times about one video; the first answer, as {"id", "results"}."""
    with open(video_path, "rb") as f:
        body = query_body(f.read(), sop)
    entry["queries"] = []
    docs = []
    for r in range(repeat):
        try:
            answer, took, tries = ask(session, body, max(deadline, time.time() + 120), seen)
        except RuntimeError as e:
            entry["queries"].append({"error": str(e)})
            log(f"    FAILED: {e}")
            break
        doc = verdict_doc(answer, stem)
        entry["queries"].append({"seconds": round(took, 1), "tries": tries,
                                 "server_ms": answer.get("query_response_time_ms"),
                                 "verdicts": len((doc or {}).get("results") or [])})
        if doc is None:
            log(f"    query {r + 1}: no verdict document in the answer: {str(answer)[:200]}")
            continue
        docs.append(doc)
        log(f"    query {r + 1}: {took:.1f} s" + (f" ({tries} tries)" if tries > 1 else "") + "  "
            + "  ".join(f"{x.get('step')}:{x.get('status')}" for x in doc.get("results") or []))
    if len(docs) > 1:
        entry["identical"] = all(d.get("results") == docs[0].get("results") for d in docs)
        log(f"    {len(docs)} answers identical: {entry['identical']}")
    return docs[0] if docs else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", nargs="+", help="clip stems (from --list)")
    ap.add_argument("--all", action="store_true", help="every mp4 in --dir")
    ap.add_argument("--dir", default="data/clips")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sop", default=DEFAULT_SOP_PATH)
    ap.add_argument("--out-dir", default=None,
                    help=f"default {OUT_DIR}/, or {OUT_DIR}_c<chunk>[o<overlap>]/ with --chunk")
    ap.add_argument("--chunk", type=float, default=0, metavar="SECONDS",
                    help="send each clip as chunks of this length; merge with merge_chunks.py")
    ap.add_argument("--overlap", type=float, default=0, metavar="SECONDS",
                    help="seconds each chunk shares with the next (with --chunk)")
    ap.add_argument("--dry-run", action="store_true",
                    help="cut and encode, print what would be sent, call nothing")
    ap.add_argument("--fresh", action="store_true", help="re-run clips that already have an output")
    ap.add_argument("--repeat", type=int, default=1, help="queries per clip, to check determinism")
    ap.add_argument("--max-frames", type=int, default=64)
    ap.add_argument("--max-new-tokens", type=int, default=api.DEFAULT_MAX_NEW_TOKENS)
    ap.add_argument("--size", type=int, default=224)
    ap.add_argument("--blueprint", default="tva", help="the blueprint to copy (default: tva)")
    ap.add_argument("--session", metavar="ID", help="query this running serving session; no new load")
    ap.add_argument("--keep", action="store_true",
                    help="leave the session and its deployment running, for --session")
    ap.add_argument("--ready-timeout", type=int, default=1800,
                    help="seconds to wait for the first answer (model load is ~7-12 min)")
    add_background_flag(ap, default_log="logs/sweep_serve.log")
    args = ap.parse_args()
    maybe_detach(args)

    available = sorted(glob.glob(os.path.join(args.dir, "*.mp4")))
    if args.list:
        for p in available:
            stem = os.path.splitext(os.path.basename(p))[0]
            print(f"  {stem:<32s} {label_str(stem)}")
        return
    if args.all:
        clips = available
    elif args.clips:
        clips = [resolve_clip(c, args.dir) or sys.exit(f"no such clip: {c}")
                 for arg in args.clips for c in arg.split(",") if c.strip()]
    else:
        sys.exit("pass --clips <stems...> or --all (or --list)")

    if args.overlap and not args.chunk:
        sys.exit("--overlap needs --chunk")
    if args.chunk and args.overlap >= args.chunk:
        sys.exit("--overlap must be shorter than --chunk")
    if args.out_dir is None:
        args.out_dir = OUT_DIR + (f"_c{args.chunk:g}" + (f"o{args.overlap:g}" if args.overlap else "")
                                  if args.chunk else "")

    sop = sop_text(load_sop(args.sop))
    stems = {p: os.path.splitext(os.path.basename(p))[0] for p in clips}
    if args.chunk:
        # Done means every chunk of the CURRENT cut has an answer, so a clip whose cut
        # changed since an earlier run re-asks only the chunks that changed.
        plans = {p: cut_chunks(p, args.chunk, args.overlap, sop) for p in clips}
        if not args.fresh:
            for p, plan in plans.items():
                resume(output_path(args, stems[p]), plan)
        todo = [p for p in clips if any(c.get("rows") is None for c in plans[p]["chunks"])]
        n_queries = sum(c.get("rows") is None for p in todo for c in plans[p]["chunks"])
    else:
        todo = [p for p in clips if args.fresh or not os.path.exists(output_path(args, stems[p]))]
    if not todo:
        sys.exit(f"nothing to run: all {len(clips)} clip(s) already have an output in "
                 f"{args.out_dir}/ (--fresh to re-run, or --out-dir)")
    if not args.chunk:
        shrunk = {p: shrink(p, sop) for p in todo}
        n_queries = len(todo)
    if args.dry_run:
        for p in todo:
            if args.chunk:
                print(f"{stems[p]}  {len(plans[p]['chunks'])} chunks")
                for c in plans[p]["chunks"]:
                    print(f"    {c['offset']:5.1f}-{c['offset'] + c['duration']:5.1f} s  "
                          f"{c['encode']:16} {c['bytes']:>9,} B")
            else:
                print(f"{stems[p]}  {shrunk[p]}  {os.path.getsize(shrunk[p]):,} B")
        print(f"\n{len(todo)} clip(s), {n_queries * args.repeat} queries -> {args.out_dir}/")
        return

    api.load_dotenv()
    log(f"{len(todo)} clip(s), {n_queries * args.repeat} queries, SOP {args.sop}")
    os.makedirs(os.path.dirname(output_path(args, "x")), exist_ok=True)

    seen: set = set()
    setting = None
    t0 = time.time()
    if args.session:
        session = args.session
        deployment = api.request("GET", api.sessions_url(session)).get("deployment_id")
    else:
        values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens,
                  "size": args.size}
        rec = project.agent(values, "serving", source=args.blueprint)
        setting, deployment = rec["key"], rec["serving"]
        session = api.open_session(deployment)["id"]
        project.record_session(session, setting=setting, kind="serving")
        log(f"{setting} · serving deployment {deployment} · session {session}")
    sweep_log = {"session": session, "deployment": deployment, "setting": setting,
                 "sop": args.sop, "clips": []}
    try:
        api.wait_running(session, timeout_s=args.ready_timeout)
        log(f"session running after {time.time() - t0:.0f} s")
        deadline = t0 + args.ready_timeout     # only the first answer may wait on the load
        for i, path in enumerate(todo, 1):
            stem = stems[path]
            log(f"[{i}/{len(todo)}] {stem}  {label_str(stem)}")
            if not args.chunk:
                entry = {"clip": stem, "sent": shrunk[path]}
                sweep_log["clips"].append(entry)
                doc = query_video(session, shrunk[path], sop, stem, args.repeat, deadline, seen, entry)
                if doc:
                    with open(output_path(args, stem), "w") as f:
                        f.write(json.dumps(doc) + "\n")     # one line, like batch: evaluate.py reads JSON lines
                continue
            plan = plans[path]
            entry = {"clip": stem, "chunks": []}
            sweep_log["clips"].append(entry)
            try:
                for c in plan["chunks"]:
                    if c.get("rows") is not None:
                        continue                    # answered by an earlier run
                    c_entry = {"offset": c["offset"], "encode": c["encode"]}
                    entry["chunks"].append(c_entry)
                    log(f"  chunk {c['offset']:5.1f}-{c['offset'] + c['duration']:5.1f} s  ({c['encode']}, {c['bytes']:,} B)")
                    doc = query_video(session, c["file"], sop, stem, args.repeat, deadline, seen, c_entry)
                    c["rows"] = doc["results"] if doc else None
            finally:
                if any(c.get("rows") is not None for c in plan["chunks"]):
                    with open(output_path(args, stem), "w") as f:
                        json.dump({"clip": stem, "sop": args.sop, **plan}, f, indent=2)
    except AgentGone as e:
        sweep_log["stopped"] = str(e)
        log(f"STOPPED: the session no longer answers ({e}).\n"
            f"  Answers so far are saved; run the same command again to resume where it stopped.")
    finally:
        if args.keep:
            log(f"session {session} left running (--session {session}); closed after 30 idle min, "
                f"and its deployment {deployment} stays up until cleanup.py")
        else:
            log(release(session, deployment, setting))
        sweep_log["seconds"] = round(time.time() - t0)
        with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as f:
            json.dump(sweep_log, f, indent=2)
    if args.chunk:
        log(f"done in {sweep_log['seconds']} s. merge and score it:  python3 merge_chunks.py {args.out_dir}")
    else:
        log(f"done in {sweep_log['seconds']} s. score it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


if __name__ == "__main__":
    main()
