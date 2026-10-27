#!/usr/bin/env python3
"""sweep.py over agent serving: load TVA once, then one /query per clip.

    python3 sweep_serve.py --all                       # every clip, one agent
    python3 sweep_serve.py --clips 1_pass_2_pass_3_pass_A --repeat 3
    python3 sweep_serve.py --all --keep                # leave the agent loaded
    python3 sweep_serve.py --all --agent agt_…         # reuse it: no model load
    python3 evaluate.py eval_serving/tva-output-*.json

    python3 sweep_serve.py --all --chunk 4 --dry-run   # cut, print the plan, call nothing
    python3 sweep_serve.py --all --chunk 10 --overlap 4
    python3 merge_chunks.py eval_serving_c10o4         # merge the chunks per rule, score each

With --chunk, each clip goes as several shorter videos instead of one, each
verified against the whole SOP, and every chunk's raw answer is saved to
<out-dir>/chunks/<clip>.json for merge_chunks.py. A chunk of 4 s or less fits
the cap as the clip's own bytes; longer ones are re-encoded, more lightly than
a whole clip.

Batch (sweep.py) pays ~7-12 min of model download and load on EVERY clip.
Serving runs the same `tva` bundle with {"mode": "serving"}: the agent loads
once and answers POST /agents/instances/{id}/query for each clip, so twelve
clips cost one load plus twelve answers instead of twelve loads.

Limits today (dev and staging only):
  - one GPU agent at a time on dev; another is refused while it's busy
  - the video goes inline (`"file_ids": ["base64:<mp4>"]`) and the whole body is
    capped at 2 MiB, so every clip is re-encoded to fit, into data/clips-serving/.
    TVA samples 64 frames at 224 px, so 640x480 at CRF 28 loses little, but the
    bytes differ from batch's and verdicts can too
  - each query has a 58 s budget, and the first query to a fresh agent can 504
    (PLDEV-2432); it is retried
  - an agent with no query for 20 minutes is released

The agent is cancelled at the end, Ctrl-C included, unless --keep: it holds
dev's only GPU worker.
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
from background import add_background_flag, maybe_detach
from run_tva_example import DEFAULT_SOP_PATH, load_sop, resolve_clip, sop_text
from sweep import label_str

OUT_DIR = "eval_serving"
CLIP_CACHE = "data/clips-serving"
BODY_CAP = 2 * 1024 * 1024
# A chunk with fewer frames than the blueprint's max_frames ceiling (64) can fail the
# agent's video preprocessing, and a graph failure ends the served agent: on dev
# (2026-10-26) a 35-frame tail chunk did, and a 40-frame one took 10x longer to answer.
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


def new_logs(agent_id: str, seen: set) -> None:
    status, body = api.request("GET", f"{api.agents_base()}/instances/{agent_id}/logs?limit=500",
                               allow_status=True)
    if status != 200 or not isinstance(body, dict):
        return
    for row in sorted(body.get("data") or [], key=lambda r: str(r.get("created_at"))):
        key = (row.get("created_at"), row.get("event_type"), row.get("message"))
        if key not in seen:
            seen.add(key)
            log(f"  {str(row.get('level')):7} {str(row.get('event_type')):28} {row.get('message')}")


def wait_running(agent: str, seen: set, timeout_s: int) -> None:
    t0 = time.time()
    while (status := api.request("GET", f"{api.agents_base()}/instances/{agent}").get("status")) != "running":
        new_logs(agent, seen)
        if status in ("failed", "completed", "cancelled", "canceled") or time.time() - t0 > timeout_s:
            sys.exit(f"agent {agent} is {status}, not running")
        time.sleep(10)


class AgentGone(Exception):
    """The agent no longer answers: cancelled, or released after 20 idle minutes."""


QUERY_TIMEOUT = 90      # the platform's budget is 58 s; past this the request is lost
QUERY_RETRIES = 3


def ask(agent: str, body: dict, deadline: float, seen: set) -> tuple[dict, float, int]:
    """POST /query, retrying 5xx and timeouts (the cold-agent 504).

    Until `deadline` (the model load) it retries without limit; after that, a few
    times. A query that hangs never reaches the agent, and an agent without a
    query for 20 minutes is released, so a stuck request must not be waited out.
    """
    tries = 0
    while True:
        tries += 1
        t = time.time()
        code, answer = api.request("POST", f"{api.agents_base()}/instances/{agent}/query",
                                   body=body, allow_status=True, retries=0, timeout=QUERY_TIMEOUT)
        took = time.time() - t
        if code == 200:
            return answer, took, tries
        new_logs(agent, seen)
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


def query_video(agent: str, video_path: str, sop: str, stem: str, repeat: int,
                deadline: float, seen: set, entry: dict) -> dict | None:
    """Ask `repeat` times about one video; the first answer, as {"id", "results"}."""
    with open(video_path, "rb") as f:
        body = query_body(f.read(), sop)
    entry["queries"] = []
    docs = []
    for r in range(repeat):
        try:
            answer, took, tries = ask(agent, body, max(deadline, time.time() + 120), seen)
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
    ap.add_argument("--agent", metavar="ID", help="query this running serving agent; no new load")
    ap.add_argument("--keep", action="store_true", help="leave the agent running for --agent")
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
    if ".dev." not in api.api_base() and "staging" not in api.api_base():
        sys.exit(f"agent serving is on dev and staging only; ATAI_API_ENDPOINT is {api.api_base()}")
    log(f"{len(todo)} clip(s), {n_queries * args.repeat} queries, SOP {args.sop}")
    os.makedirs(os.path.dirname(output_path(args, "x")), exist_ok=True)

    seen: set = set()
    bundle = None
    t0 = time.time()
    if args.agent:
        agent = args.agent
    else:
        values = {"max_frames": args.max_frames, "max_new_tokens": args.max_new_tokens}
        bundle = api.create_bundle("tva", f"tva serving sweep {api.run_suffix()}", values)["id"]
        agent = api.request("POST", f"{api.agents_base()}/bundles/{bundle}/run",
                            body={"mode": "serving"})["id"]
        log(f"bundle {bundle} · agent {agent} (serving) · {values}")
    sweep_log = {"agent": agent, "bundle": bundle, "sop": args.sop, "clips": []}
    try:
        wait_running(agent, seen, args.ready_timeout)
        log(f"agent running after {time.time() - t0:.0f} s")
        deadline = t0 + args.ready_timeout     # only the first answer may wait on the load
        for i, path in enumerate(todo, 1):
            stem = stems[path]
            log(f"[{i}/{len(todo)}] {stem}  {label_str(stem)}")
            if not args.chunk:
                entry = {"clip": stem, "sent": shrunk[path]}
                sweep_log["clips"].append(entry)
                doc = query_video(agent, shrunk[path], sop, stem, args.repeat, deadline, seen, entry)
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
                    doc = query_video(agent, c["file"], sop, stem, args.repeat, deadline, seen, c_entry)
                    c["rows"] = doc["results"] if doc else None
            finally:
                if any(c.get("rows") is not None for c in plan["chunks"]):
                    with open(output_path(args, stem), "w") as f:
                        json.dump({"clip": stem, "sop": args.sop, **plan}, f, indent=2)
    except AgentGone as e:
        sweep_log["stopped"] = str(e)
        log(f"STOPPED: the agent no longer answers ({e}).\n"
            f"  Answers so far are saved; run the same command again to resume where it stopped.")
    finally:
        if args.keep:
            log(f"agent {agent} left running (--agent {agent}); released after 20 idle min")
        else:
            api.cancel(agent)
            if bundle:
                api.request("DELETE", f"{api.agents_base()}/bundles/{bundle}", raw=True, allow_status=True)
            log(f"cancelled {agent}" + (f", deleted {bundle}" if bundle else ""))
        sweep_log["seconds"] = round(time.time() - t0)
        with open(os.path.join(args.out_dir, "sweep-log.json"), "w") as f:
            json.dump(sweep_log, f, indent=2)
    if args.chunk:
        log(f"done in {sweep_log['seconds']} s. merge and score it:  python3 merge_chunks.py {args.out_dir}")
    else:
        log(f"done in {sweep_log['seconds']} s. score it:  python3 evaluate.py {args.out_dir}/tva-output-*.json")


if __name__ == "__main__":
    main()
