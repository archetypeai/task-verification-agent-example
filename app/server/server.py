#!/usr/bin/env python3
"""Task Verification App: the API behind the SvelteKit UI in app/, over TVA in serving.

    cd app && npm run build && python3 server/server.py    # http://127.0.0.1:8765
    python3 server/server.py --port 9000 --keep

Start the agent once (the model loads in ~10-12 min), then verify any video
against any SOP in seconds per query. The serving deployment holds a GPU
worker, so it is stopped when the server exits unless --keep is given.

The API key and endpoint come from app/.env, else the repo's .env (read as a
pair), and can be overridden from the UI. With no key in either, enter it in the
UI; the endpoint then defaults to DEFAULT_ENDPOINT.

Stdlib only, like the rest of the repo. It reuses tva_api, tva_project and
sweep_serve from the repo root, with its own state in app/state/ and its own
project, so it never touches the CLI scripts' state/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import mimetypes
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(APP_DIR)
BUILD = os.path.realpath(os.path.join(APP_DIR, "build"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # the shared modules use paths relative to the repo root

import tva_api as api  # noqa: E402
import tva_project as project  # noqa: E402
import merge_chunks  # noqa: E402
import sweep_serve  # noqa: E402
from evaluate import by_step  # noqa: E402

DEFAULT_RULE = "any"

APP_ENV = os.path.join(APP_DIR, ".env")
UPLOADS = os.path.join(APP_DIR, "uploads")
RUNS = os.path.join(APP_DIR, "runs")
DEFAULT_ENDPOINT = "https://platform.u1.archetypeai.app/api"
DEFAULT_VALUES = {"max_frames": 64, "size": 224, "max_new_tokens": api.DEFAULT_MAX_NEW_TOKENS}

project.PROJECT = "TVA App"


def use_state_for(endpoint: str, key: str) -> None:
    """One state file per endpoint AND key.

    Projects, copies, packages and deployments belong to an organization, and a key
    belongs to one; ids saved under another key's organization answer 404. The
    folder is named with a short hash of the key, never the key itself. A new key's
    first start finds its organization's project by name, or creates it."""
    slug = re.sub(r"[^a-z0-9]+", "-", endpoint.lower()).strip("-")
    if key:
        slug += "-" + hashlib.sha256(key.encode()).hexdigest()[:8]
    project.STATE_DIR = os.path.join(APP_DIR, "state", slug)
    project.STATE = os.path.join(project.STATE_DIR, "lifecycle.json")


def read_env(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("\"'")
    return out


# Where the key in use came from: "app/.env", ".env", "app" (entered in the UI) or None.
KEY_SOURCE: str | None = None


def load_config() -> None:
    """The key and endpoint from app/.env, else from the repo's .env; the UI can override both.

    They are read as a pair from the first file that has a key, because a key only
    works on the deployment that issued it. A file with a key and no endpoint means
    the default endpoint. With no key anywhere, the endpoint is app/.env's or the
    default, and the key is entered in the UI."""
    global KEY_SOURCE
    endpoint, key = read_env(APP_ENV).get("ATAI_API_ENDPOINT") or DEFAULT_ENDPOINT, ""
    for path, label in ((APP_ENV, "app/.env"), (os.path.join(ROOT, ".env"), ".env")):
        env = read_env(path)
        if env.get("ATAI_API_KEY"):
            key, KEY_SOURCE = env["ATAI_API_KEY"], label
            endpoint = env.get("ATAI_API_ENDPOINT") or DEFAULT_ENDPOINT
            break
    os.environ["ATAI_API_ENDPOINT"] = endpoint.rstrip("/")
    # Set even when empty: tva_api.load_dotenv() fills only what is absent.
    os.environ["ATAI_API_KEY"] = key
    use_state_for(os.environ["ATAI_API_ENDPOINT"], key)


def check_startup_key() -> None:
    """Report whether a key from .env works, and reattach to a running session if so."""
    if not os.environ.get("ATAI_API_KEY"):
        return
    ok, message = check_key()
    AGENT.note(f"API key from {KEY_SOURCE} on {os.environ['ATAI_API_ENDPOINT']}: {message}",
               "info" if ok else "warn")
    if ok:
        key_ready()


def key_ready() -> None:
    """A key was just accepted: adopt its old state, reattach, and stop orphaned GPUs."""
    adopt_legacy_state()
    AGENT.attach()
    if AGENT.status in ("stopped", "error"):
        stop_orphaned_deployments()


def adopt_legacy_state() -> None:
    """Move a state folder from before folders were per key to this key, if it is this key's.

    Earlier versions kept one folder per endpoint. Its ids belong to whichever
    key's organization made them: adopt it only if this key can see its project."""
    legacy = re.sub(r"-[0-9a-f]{8}$", "", project.STATE_DIR)
    legacy_state = os.path.join(legacy, "lifecycle.json")
    if legacy == project.STATE_DIR or os.path.exists(project.STATE) or not os.path.exists(legacy_state):
        return
    try:
        pid = json.load(open(legacy_state)).get("project")
    except (OSError, ValueError):
        return
    if not pid:
        return
    status, _ = api.request("GET", f"{api.api_base()}/projects/{pid}", allow_status=True,
                            retries=0, timeout=20)
    if status == 200:
        os.rename(legacy, project.STATE_DIR)
        AGENT.note(f"adopted the saved state for project {pid} (made with this key's organization)")


def drop_stale_state() -> None:
    """Set aside a saved state whose project this key can no longer see.

    tva_project trusts the saved project id, so a deleted project (or a state from
    another organization) would otherwise fail every start with a 404."""
    if not os.path.exists(project.STATE):
        return
    pid = json.load(open(project.STATE)).get("project")
    if not pid:
        return
    status, _ = api.request("GET", f"{api.api_base()}/projects/{pid}", allow_status=True,
                            retries=1, timeout=20)
    if status == 404:
        aside = f"{project.STATE}.stale-{time.strftime('%Y%m%dT%H%M%S')}"
        os.rename(project.STATE, aside)
        AGENT.note(f"project {pid} is not visible to this key; set its saved state aside "
                   f"({os.path.relpath(aside, APP_DIR)}) and starting fresh", "warn")


def stop_orphaned_deployments() -> None:
    """Stop serving deployments this app recorded that are up with no session of ours running.

    A server killed without its shutdown (a closed terminal, kill -9) leaves its
    deployment holding a GPU worker until someone stops it."""
    try:
        state = call(project.load_state)
    except RuntimeError:
        return
    for setting, rec in state.get("settings", {}).items():
        dpl = rec.get("serving")
        if not dpl:
            continue
        status, obj = api.request("GET", api.deployments_url(dpl), allow_status=True,
                                  retries=0, timeout=20)
        if status == 200 and obj.get("status") not in ("stopped", "failed"):
            AGENT.note(f"stopping {dpl}: a serving deployment left up by an earlier run "
                       f"({obj.get('status')}), holding a GPU with no session of this app", "warn")
            api.end_deployment(dpl)
        if status in (200, 404):
            project.forget(setting, "serving")


def set_config(key: str, endpoint: str) -> tuple[bool, str]:
    """Use this key and endpoint if the platform accepts the pair; else keep the current ones.

    An empty value keeps the current one. Keys are per deployment, so a new
    endpoint is checked with the key too."""
    old = os.environ.get("ATAI_API_KEY", ""), os.environ.get("ATAI_API_ENDPOINT", "")
    key = key or old[0]
    endpoint = (endpoint or old[1]).rstrip("/")
    if not re.match(r"^https?://[^/\s]+", endpoint):
        return False, f"not an http(s) URL: {endpoint}"
    if (key, endpoint) == old:
        return True, "unchanged"
    if not key:
        return False, "enter an API key"
    if AGENT.status not in ("stopped", "error"):
        return False, "stop the agent before changing the API key or endpoint"
    os.environ["ATAI_API_KEY"], os.environ["ATAI_API_ENDPOINT"] = key, endpoint
    ok, message = check_key()
    if not ok:
        os.environ["ATAI_API_KEY"], os.environ["ATAI_API_ENDPOINT"] = old
        return False, message
    use_state_for(endpoint, key)
    global KEY_SOURCE
    if key != old[0]:
        KEY_SOURCE = "app"
    AGENT.reset()
    key_ready()
    return True, message


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "set"


def check_key() -> tuple[bool, str]:
    status, body = api.request("GET", f"{api.agents_base()}/blueprints/tva", allow_status=True,
                               retries=1, timeout=20)
    if status == 200:
        return True, "connected; the tva blueprint is visible"
    if status in (401, 403):
        return False, f"the API key was rejected ({status}); keys are per deployment"
    if status == 404:
        return False, "connected, but the tva blueprint is not visible to this key"
    return False, f"could not reach the endpoint ({status or 'network error'})"


def call(fn, *a, **kw):
    """Run a shared-module call; their errors are sys.exit(msg)."""
    try:
        return fn(*a, **kw)
    except SystemExit as e:
        raise RuntimeError(str(e.code))


class Agent:
    """The one serving session this app drives."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gen = 0  # counts starts; see _start
        self.reset()

    def reset(self, status: str = "stopped", error: str | None = None) -> None:
        self.status, self.error = status, error
        self.session = self.deployment = self.setting = None
        self.values = dict(DEFAULT_VALUES)
        self.started_at = self.running_at = None
        self.last_query_at = None
        self.events: list[dict] = []
        self._seen: set = set()
        # While starting: "preparing", "placing" (deployment waiting for a GPU), "loading".
        self.phase: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {k: getattr(self, k) for k in (
                "status", "error", "phase", "session", "deployment", "setting", "values",
                "started_at", "running_at", "last_query_at")} | {
                "events": self.events[-40:], "now": time.time()}

    def note(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.events.append({"t": time.time(), "level": level, "message": message})

    def poll_events(self) -> None:
        if not self.session:
            return
        status, body = api.request("GET", f"{api.sessions_url(self.session)}/events?limit=100",
                                   allow_status=True, retries=0, timeout=20)
        if status != 200 or not isinstance(body, dict):
            return
        for row in reversed(body.get("data") or []):
            key = (row.get("created_at"), row.get("message"))
            if key not in self._seen:
                self._seen.add(key)
                self.note(str(row.get("message")), str(row.get("level", "info")))

    def start(self, values: dict) -> None:
        with self.lock:
            if self.status in ("starting", "running", "stopping"):
                raise RuntimeError(f"the agent is already {self.status}")
            self.reset("starting")
            self.values = values
            self.started_at = time.time()
            self.phase = "preparing"
            self.gen += 1
            gen = self.gen
        threading.Thread(target=self._start, args=(gen,), daemon=True).start()

    def _start(self, gen: int) -> None:
        """Bring up a serving session. `gen` identifies this start.

        A start cancelled while it waits (Cancel, then Start again) keeps running
        in its thread until the platform call it is in returns. It must then step
        aside: not open a session, and not stop a deployment a newer start is
        using."""
        current = lambda: self.gen == gen and self.status == "starting"  # noqa: E731
        try:
            self.note("preparing project, blueprint copy and package")
            drop_stale_state()
            rec = call(project.agent, self.values, "serving")
            if not current():
                if self.gen == gen:  # stopped, and no newer start needs the deployment
                    api.end_deployment(rec["serving"])
                    project.forget(rec["key"], "serving")
                return
            self.setting, self.deployment = rec["key"], rec["serving"]
            self.note(f"setup: project {project.load_state().get('project')}, blueprint copy "
                      f"{rec['blueprint']} ({rec['key']}), package {rec['package']}")
            self.note(f"serving deployment {self.deployment} ready")
            self.session = call(api.open_session, self.deployment)["id"]
            call(project.record_session, self.session, setting=self.setting, kind="serving")
            self.phase = "loading"
            self.note(f"session {self.session} opened")
            t0 = last_beat = time.time()
            seen = None
            while True:
                if not current():
                    return  # stopped while loading
                ses = call(api.request, "GET", api.sessions_url(self.session))
                self.poll_events()
                if ses["status"] != seen:
                    seen = ses["status"]
                    self.note(f"session {self.session}: {seen} ({fmt_elapsed(time.time() - t0)})")
                elif time.time() - last_beat >= HEARTBEAT_S:
                    last_beat = time.time()
                    self.note(f"session {self.session}: still {seen} ({fmt_elapsed(time.time() - t0)})")
                if ses["status"] == "running":
                    break
                if ses["status"] in api.TERMINAL:
                    raise RuntimeError(f"session {ses['status']}: {ses.get('error')}")
                time.sleep(10)
            with self.lock:
                self.status, self.running_at = "running", time.time()
                self.last_query_at = self.running_at
                self.phase = None
            self.note("agent is running; ready to verify")
        except Exception as e:  # noqa: BLE001
            if self.gen != gen:
                return  # a newer start owns the agent now
            self.note(str(e), "error")
            self._release()
            with self.lock:
                self.status, self.error, self.phase = "error", str(e), None

    def attach(self) -> None:
        """Adopt a serving session left running by an earlier server (--keep)."""
        try:
            sessions = call(project.sessions)
        except Exception:  # noqa: BLE001
            return
        for sid, info in sorted(sessions.items(), key=lambda kv: kv[1].get("created_at", ""),
                                reverse=True):
            if info.get("kind") != "serving":
                continue
            status, ses = api.request("GET", api.sessions_url(sid), allow_status=True, retries=0)
            if status == 200 and ses.get("status") == "running":
                with self.lock:
                    self.session, self.setting = sid, info.get("setting")
                    self.deployment = ses.get("deployment_id")
                    self.status = "running"
                    self.started_at = self.running_at = self.last_query_at = time.time()
                self.note(f"attached to running session {sid}")
                return

    def stop(self) -> None:
        with self.lock:
            if self.status in ("stopped", "stopping"):
                return
            self.status = "stopping"
        self.note("stopping: cancelling the session and stopping the deployment")
        self._release()
        with self.lock:
            self.reset("stopped")

    def _release(self) -> None:
        if self.session:
            try:
                sweep_serve.release(self.session, self.deployment, self.setting)
            except Exception as e:  # noqa: BLE001
                self.note(f"release failed: {e}", "error")

    def query(self, video_path: str, sop: str) -> tuple[dict, float]:
        if self.status != "running":
            raise RuntimeError("start the agent first")
        body = sweep_serve.query_body(open(video_path, "rb").read(), sop)
        try:
            answer, took, _ = sweep_serve.ask(self.session, body, time.time() + 60, self._seen)
        except sweep_serve.AgentGone as e:
            with self.lock:
                self.reset("stopped", f"the session closed ({e}); start the agent again")
            raise RuntimeError(self.error)
        with self.lock:
            self.last_query_at = time.time()
        return answer, took


AGENT = Agent()

_deployment_ready = api.deployment_ready


def _placing(deployment_id: str, *a, **kw):
    """tva_project waits here for a serving deployment to get a GPU; tell the page."""
    if AGENT.status == "starting":
        AGENT.phase = "placing"
        AGENT.note(f"waiting for serving deployment {deployment_id}: the platform places it on a "
                   f"GPU, then downloads and loads the model weights (about 10-12 min)")
    return _deployment_ready(deployment_id, *a, **kw)


api.deployment_ready = _placing

HEARTBEAT_S = 60


def _logged_wait_for(url: str, status: str, waiting: str, timeout_s: int, every_s: int = 5,
                     exit: bool = True) -> dict:
    """tva_api._wait_for, writing to the activity log while it waits.

    Package builds and GPU placement each take minutes and report nothing, so this
    notes what is being waited on, every status change, and the platform's status
    once a minute. Same arguments, results and errors as the original."""
    what = url.rstrip("/").split("/")[-2].rstrip("s")       # package / deployment / session
    obj = api.request("GET", url)
    t0 = last_beat = time.time()
    seen = obj["status"]
    if seen == waiting:
        AGENT.note(f"{what} {obj['id']}: {seen}; waiting for it to be {status}")
    while obj["status"] == waiting:
        if time.time() - t0 > timeout_s:
            if not exit:
                return obj
            sys.exit(f"{obj['id']} still {waiting} after {timeout_s} s")
        time.sleep(every_s)
        obj = api.request("GET", url)
        if obj["status"] != seen:
            seen = obj["status"]
            AGENT.note(f"{what} {obj['id']}: {seen} after {fmt_elapsed(time.time() - t0)}")
        elif time.time() - last_beat >= HEARTBEAT_S:
            last_beat = time.time()
            AGENT.note(f"{what} {obj['id']}: still {seen} ({fmt_elapsed(time.time() - t0)}); "
                       f"the platform reports no progress while it waits")
    if obj["status"] != status and exit:
        sys.exit(f"{obj['id']} is {obj['status']}, not {status}: {obj.get('error')}")
    return obj


api._wait_for = _logged_wait_for


def fmt_elapsed(s: float) -> str:
    m, s = divmod(int(s), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"
# tva_project reports each step (copy, package build, deployment) through its
# log(); route it to the UI's activity log as well as the terminal.
_project_log = project.log
project.log = lambda msg: (_project_log(msg), AGENT.note(msg))


def as_mp4(src: str) -> str:
    """The upload as an mp4, converted once if it is another container."""
    if src.lower().endswith(".mp4"):
        return src
    out = os.path.splitext(src)[0] + ".conv.mp4"
    if not os.path.exists(out):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-crf", "23",
                        "-an", "-movflags", "+faststart", out], check=True)
    return out


def step_rows(steps: list[str], rows: dict[int, dict]) -> list[dict]:
    """One row per SOP step (1-based), in SOP order, whatever the model reported."""
    return [{**(rows.get(i) or {"status": "NOT REPORTED"}), "step": i, "text": text}
            for i, text in enumerate(steps, 1)]


def query_rows(path: str, sop: str) -> tuple[list[dict], float]:
    answer, took = AGENT.query(path, sop)
    doc = sweep_serve.verdict_doc(answer, "x")
    if doc is None:
        raise RuntimeError(f"no verdicts in the answer: {str(answer)[:300]}")
    return doc["results"], took


class Cancelled(Exception):
    pass


class Job:
    """One verification: a single query (whole video) or one per chunk (long video)."""

    def __init__(self, upload_id: str, filename: str, steps: list[str], chunked: dict | None):
        self.id = uuid.uuid4().hex[:10]
        self.upload_id, self.filename, self.steps, self.chunked = upload_id, filename, steps, chunked
        self.status, self.error, self.result = "running", None, None
        self.phase, self.done, self.total = "preparing the video", 0, 1
        self.cancelled = False

    def snapshot(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "status", "error", "result", "phase", "done", "total")}

    def run(self) -> None:
        try:
            src = next((os.path.join(UPLOADS, f) for f in os.listdir(UPLOADS)
                        if f.startswith(self.upload_id + ".")), None)
            if not src:
                raise RuntimeError("upload not found; choose the video again")
            sop = "\n".join(self.steps) + "\n"
            src = as_mp4(src)
            self.result = self.chunks(src, sop) if self.chunked else self.whole(src, sop)
            self.result.update({"id": self.upload_id, "video": self.filename,
                                "setting": AGENT.setting, "created_at": time.time()})
            os.makedirs(RUNS, exist_ok=True)
            with open(os.path.join(RUNS, f"{self.upload_id}-{self.id}.json"), "w") as f:
                json.dump(self.result, f, indent=2)
            self.status = "done"
        except Cancelled:
            self.status, self.error = "cancelled", "cancelled"
        except SystemExit as e:
            self.status, self.error = "error", str(e.code)
        except Exception as e:  # noqa: BLE001
            self.status, self.error = "error", str(e)

    def whole(self, src: str, sop: str) -> dict:
        sent = call(sweep_serve.shrink, src, sop)
        self.phase = "querying"
        rows, took = query_rows(sent, sop)
        self.done = 1
        return {"mode": "whole", "seconds": round(took, 1),
                "steps": step_rows(self.steps, by_step({"results": rows})), "raw": rows}

    def ask(self, path: str, sop: str) -> tuple[list[dict], float]:
        if self.cancelled:
            raise Cancelled()
        rows, took = query_rows(path, sop)
        self.done += 1
        return rows, took

    def chunk_rows(self, c: dict) -> tuple[dict[int, dict], float]:
        """One chunk's verdicts as {1-based step: row}, one row per SOP step at most.

        All steps at once: one query against the whole SOP. The model's `step` is the
        row's position in its answer, so a repeated or merged row shifts every row
        after it; by_step() collapses repeats, and steps past the SOP are dropped.
        One step per query: a query per step, each with a one-line SOP, so a row
        cannot land on another step's number."""
        n = len(self.steps)
        if not self.chunked["per_step"]:
            rows, took = self.ask(c["file"], "\n".join(self.steps) + "\n")
            return {k: r for k, r in by_step({"results": rows}).items() if k <= n}, took
        out, took = {}, 0.0
        for k, step in enumerate(self.steps, 1):
            self.phase = f"{c['label']}, step {k} of {n}"
            rows, t = self.ask(c["file"], f"Step 1: {stepless(step)}\n")
            took += t
            if (row := by_step({"results": rows}).get(1)) is not None:
                out[k] = row
        return out, took

    def chunks(self, src: str, sop: str) -> dict:
        seconds, overlap = self.chunked["seconds"], self.chunked["overlap"]
        n = len(self.steps)
        self.phase = "cutting the video into chunks"
        plan = call(sweep_serve.cut_chunks, src, seconds, overlap, sop)
        per_chunk = n if self.chunked["per_step"] else 1
        self.done, self.total = 0, len(plan["chunks"]) * per_chunk
        took = 0.0
        for i, c in enumerate(plan["chunks"]):
            c["label"] = (f"chunk {i + 1} of {len(plan['chunks'])} "
                          f"({c['offset']:.0f}–{c['offset'] + c['duration']:.0f} s)")
            self.phase = c["label"]
            start = self.done
            try:
                rows, t = self.chunk_rows(c)
                took += t
                # merge_chunks wants the model's 0-based step numbering.
                c["rows"] = [{**r, "step": k - 1} for k, r in sorted(rows.items())]
            except RuntimeError as e:
                if AGENT.status != "running":  # the session is gone: nothing more will answer
                    raise
                c["rows"], c["error"] = None, str(e)
                self.done = start + per_chunk
        doc = {**plan, "clip": self.upload_id}
        evidence = chunk_evidence(plan, n)
        merged = {}
        for rule in merge_chunks.RULES:
            rows = {r["step"] + 1: r for r in merge_chunks.merge(doc, rule)["results"]}
            merged[rule] = [run_span(s, evidence[s["step"]]) for s in step_rows(self.steps, rows)]
        failed = [c["offset"] for c in plan["chunks"] if c.get("rows") is None]
        return {"mode": "chunked", "per_step": self.chunked["per_step"], "queries": self.done,
                "seconds": round(took, 1), "chunk_seconds": seconds,
                "overlap": overlap, "duration": plan["duration"],
                "chunks": [{"offset": c["offset"], "duration": c["duration"],
                            "error": c.get("error")} for c in plan["chunks"]],
                "evidence": evidence, "failed_chunks": failed, "merged": merged,
                "rule": DEFAULT_RULE, "steps": merged[DEFAULT_RULE]}


def stepless(step: str) -> str:
    return re.sub(r"^\s*step\s*\d+\s*[:.)-]\s*", "", step, flags=re.I)


def chunk_evidence(plan: dict, n: int) -> dict[int, list[dict]]:
    """{1-based step: one entry per chunk}: what each chunk said about that step, in clip time."""
    out: dict[int, list[dict]] = {k: [] for k in range(1, n + 1)}
    for i, c in enumerate(plan["chunks"]):
        rows = {r["step"] + 1: r for r in c.get("rows") or []}
        for k in out:
            r = rows.get(k)
            if r is None:
                out[k].append({"chunk": i, "offset": c["offset"], "duration": c["duration"],
                               "status": "ERROR" if c.get("rows") is None else "NOT REPORTED"})
                continue
            r = merge_chunks.to_clip_time(r, c["offset"], plan["fps"])
            out[k].append({"chunk": i, "offset": c["offset"], "duration": c["duration"],
                           "status": r.get("status"), "reason": r.get("reason"),
                           "timestamp_start": r.get("timestamp_start"),
                           "timestamp_end": r.get("timestamp_end")})
    return out


def run_span(step: dict, evidence: list[dict]) -> dict:
    """Time a merged verdict by its longest unbroken run of chunks that gave it.

    The model passes a step in chunks where it doesn't happen, too, so neither the
    span of all agreeing chunks (step 1 of the gel example: 6-523 s) nor the
    earliest of them (right for 1 of 9 steps in a one-step run) says when it
    happened. The longest run of consecutive agreeing chunks overlapped the real
    time for 9 of 9 steps there, though it can be wide. Ties go to the earliest run.
    The span of all agreeing chunks is kept as `span_all`."""
    agree = [e for e in evidence if e["status"] == step["status"]]
    step = {**step, "agreeing_chunks": len(agree), "answered_chunks":
            sum(e["status"] not in ("ERROR", "NOT REPORTED") for e in evidence)}
    if step["status"] not in ("PASSED", "FAILED") or not agree:
        return step
    if "span_all" not in step:
        step["span_all"] = [step.get("timestamp_start"), step.get("timestamp_end")]
    runs, cur = [], []
    for e in evidence:                      # one entry per chunk, in order
        if e["status"] == step["status"]:
            cur.append(e)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    best = max(runs, key=len)               # max() keeps the first of equals: the earliest
    first, last = best[0], best[-1]
    step.update(
        timestamp_start=first["timestamp_start"] if first.get("timestamp_start") is not None else first["offset"],
        timestamp_end=last["timestamp_end"] if last.get("timestamp_end") is not None
        else last["offset"] + last["duration"],
        reason=first.get("reason"), run_chunks=[e["chunk"] for e in best],
        decided_by_chunk=first["chunk"])
    return step


def retime(result: dict) -> dict:
    """Apply run_span to a long-video result, including ones saved before it existed."""
    evidence = result.get("evidence")
    if not evidence or not result.get("merged"):
        return result
    ev = {int(k): v for k, v in evidence.items()}
    result["merged"] = {rule: [run_span(st, ev[st["step"]]) for st in steps]
                        for rule, steps in result["merged"].items()}
    result["steps"] = result["merged"][result.get("rule") or DEFAULT_RULE]
    return result


JOBS: dict[str, Job] = {}


def saved_runs(limit: int = 20) -> list[dict]:
    """The most recent results in app/runs/, newest first, without their bulk."""
    if not os.path.isdir(RUNS):
        return []
    paths = sorted((os.path.join(RUNS, f) for f in os.listdir(RUNS) if f.endswith(".json")),
                   key=os.path.getmtime, reverse=True)[:limit]
    out = []
    for p in paths:
        try:
            r = json.load(open(p))
        except (OSError, ValueError):
            continue
        out.append({"name": os.path.basename(p)[:-5], "video": r.get("video"), "mode": r.get("mode", "whole"),
                    "per_step": r.get("per_step", False), "created_at": r.get("created_at"),
                    "steps": len(r.get("steps") or [])})
    return out


def start_job(b: dict) -> Job:
    if AGENT.status != "running":
        raise RuntimeError("start the agent first")
    if any(j.status == "running" for j in JOBS.values()):
        raise RuntimeError("a verification is already running")
    steps = [l for l in (b.get("sop") or "").splitlines() if l.strip()]
    if not steps:
        raise RuntimeError("the SOP has no steps")
    chunked = None
    if b.get("mode") == "chunked":
        seconds, overlap = float(b.get("chunk_seconds") or 20), float(b.get("overlap") or 0)
        if not 4 <= seconds <= 120:
            raise RuntimeError("chunk length must be 4-120 s")
        if not 0 <= overlap < seconds:
            raise RuntimeError("overlap must be at least 0 and shorter than the chunk")
        # One step per query is the default: see examples/gel-electrophoresis.
        chunked = {"seconds": seconds, "overlap": overlap, "per_step": bool(b.get("per_step", True))}
    job = Job(b["upload_id"], b.get("filename", ""), steps, chunked)
    JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # quieter: only errors
        if not str(a[1] if len(a) > 1 else "").startswith(("2", "3")):
            super().log_message(fmt, *a)

    def send_json(self, obj, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_static(self) -> None:
        """The built SvelteKit app; unknown paths fall back to its index.html."""
        rel = self.path.split("?")[0].lstrip("/") or "index.html"
        path = os.path.realpath(os.path.join(BUILD, rel))
        if not path.startswith(BUILD + os.sep) or not os.path.isfile(path):
            path = os.path.join(BUILD, "index.html")
        if not os.path.isfile(path):
            return self.send_json({"error": "the UI is not built: cd app && npm run build"}, 503)
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.path.startswith("/api/"):
            self.send_static()
        elif self.path == "/api/config":
            key = os.environ.get("ATAI_API_KEY", "")
            self.send_json({"endpoint": os.environ.get("ATAI_API_ENDPOINT"),
                            "default_endpoint": DEFAULT_ENDPOINT,
                            "key_set": bool(key), "key_hint": mask(key) if key else "",
                            "key_source": KEY_SOURCE if key else None})
        elif self.path == "/api/agent":
            self.send_json(AGENT.snapshot())
        elif self.path == "/api/runs":
            self.send_json(saved_runs())
        elif self.path.startswith("/api/runs/"):
            name = os.path.basename(self.path.split("?")[0])
            path = os.path.join(RUNS, name + ".json")
            if not re.fullmatch(r"[\w.-]+", name) or not os.path.isfile(path):
                return self.send_json({"error": "no such run"}, 404)
            self.send_json(retime(json.load(open(path))))
        elif self.path.startswith("/api/jobs/"):
            job = JOBS.get(self.path.split("/")[3])
            self.send_json(job.snapshot() if job else {"error": "no such job"}, 200 if job else 404)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/api/config":
                self.post_config()
            elif self.path == "/api/agent/start":
                b = self.body_json()
                values = {k: int(b.get(k) or v) for k, v in DEFAULT_VALUES.items()}
                if not 1 <= values["max_frames"] <= 64:
                    raise RuntimeError("max_frames must be 1-64")
                if not 224 <= values["size"] <= 2048:
                    raise RuntimeError("size must be 224-2048")
                if not os.environ.get("ATAI_API_KEY"):
                    raise RuntimeError("set an API key first")
                AGENT.start(values)
                self.send_json(AGENT.snapshot())
            elif self.path == "/api/agent/stop":
                threading.Thread(target=AGENT.stop, daemon=True).start()
                self.send_json({"ok": True})
            elif self.path.startswith("/api/upload"):
                self.post_upload()
            elif self.path == "/api/verify":
                self.send_json(start_job(self.body_json()).snapshot())
            elif self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
                job = JOBS.get(self.path.split("/")[3])
                if not job:
                    raise RuntimeError("no such job")
                job.cancelled = True
                self.send_json(job.snapshot())
            else:
                self.send_json({"error": "not found"}, 404)
        except (RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as e:
            self.send_json({"error": str(e)}, 400)
        except SystemExit as e:  # the shared modules report errors with sys.exit
            self.send_json({"error": str(e.code)}, 400)

    def post_config(self) -> None:
        """Set the API key and/or endpoint, held in memory only."""
        b = self.body_json()
        ok, message = set_config((b.get("api_key") or "").strip(), (b.get("endpoint") or "").strip())
        if not ok:
            raise RuntimeError(message)
        key = os.environ["ATAI_API_KEY"]
        self.send_json({"ok": True, "message": message,
                        "endpoint": os.environ["ATAI_API_ENDPOINT"],
                        "default_endpoint": DEFAULT_ENDPOINT,
                        "key_set": bool(key), "key_hint": mask(key) if key else "",
                            "key_source": KEY_SOURCE if key else None})

    def post_upload(self) -> None:
        name = os.path.basename(self.headers.get("X-Filename") or "video.mp4")
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise RuntimeError(f"unsupported video type {ext or '(none)'}")
        n = int(self.headers.get("Content-Length") or 0)
        upload_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        os.makedirs(UPLOADS, exist_ok=True)
        with open(os.path.join(UPLOADS, upload_id + ext), "wb") as f:
            while n > 0:
                chunk = self.rfile.read(min(n, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                n -= len(chunk)
        self.send_json({"upload_id": upload_id, "filename": name})


def idle_watch() -> None:
    """Warn before the 30-minute idle close, and notice when the session has ended.

    A closed session no longer answers, but its serving deployment keeps its GPU
    worker until it is stopped, so a closed session is released here rather than
    discovered at the next query."""
    warned = False
    while True:
        time.sleep(30)
        s = AGENT.snapshot()
        if s["status"] != "running" or not s["session"]:
            warned = False
            continue
        if s["last_query_at"] and time.time() - s["last_query_at"] > 25 * 60 and not warned:
            AGENT.note("idle 25 min: the platform closes a serving session after 30", "warn")
            warned = True
        if any(j.status == "running" for j in JOBS.values()):
            continue
        status, ses = api.request("GET", api.sessions_url(s["session"]), allow_status=True,
                                  retries=0, timeout=20)
        if status == 200 and ses.get("status") in api.TERMINAL:
            AGENT.note(f"session {s['session']} is {ses['status']} "
                       f"({ses.get('error') or 'idle timeout?'}); stopping its deployment", "warn")
            AGENT.stop()
            with AGENT.lock:
                AGENT.error = (f"The serving session ended ({ses['status']}), probably after 30 idle "
                               f"minutes. Its deployment was stopped. Start the agent again to continue.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--keep", action="store_true",
                    help="leave the serving session running on exit (it holds a GPU)")
    args = ap.parse_args()
    load_config()
    threading.Thread(target=check_startup_key, daemon=True).start()
    threading.Thread(target=idle_watch, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Task Verification App on http://{args.host}:{args.port}  (Ctrl-C to quit)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if AGENT.session and not args.keep:
            print("stopping the serving session and deployment ...")
            AGENT.stop()
        elif AGENT.session:
            print(f"left running: session {AGENT.session} (closes after 30 idle min)")


if __name__ == "__main__":
    main()
