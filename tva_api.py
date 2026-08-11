#!/usr/bin/env python3
"""Minimal stdlib-only client for the Archetype AI Agent API, for TVA.

Shared by run_tva_example.py, sweep.py and inspect_bundle.py. Auth and endpoint
come from the environment, with a local .env loaded if present:

    ATAI_API_KEY=<dev API key>
    ATAI_API_ENDPOINT=https://api.dev.u1.archetypeai.app

Adapted from the MGA example's mga_api.py. The differences are all consequences
of one thing: **TVA takes a reference procedure at runtime and MGA does not.**

  * `upload_text()` exists. The SOP is uploaded as its own `.txt` file and passed
    as a SECOND source input alongside the video, because the tva blueprint wires
    `source.text -> prepare.in` (PrepareSOPNode). MGA's blueprint sets
    `text_extensions: []` and hardcodes its instruction, so it has no equivalent.
  * `run()` takes two file ids rather than one, and the ORDER DOES NOT MATTER —
    MultiModalSource routes by the `format` field, not by position.
  * There is no fitted-artifact path, same as MGA: TVA V1 is zero-shot, and the
    blueprint pins newton-fusion:1.0 and whisper:large-v3 itself.
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# The log event types that end a run. The instance `status` field is not usable
# for this — see poll_logs().
TERMINAL_EVENTS = ("pod.terminated", "job.completed", "job.failed", "job.canceled")


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def api_base() -> str:
    load_dotenv()
    endpoint = os.environ.get("ATAI_API_ENDPOINT", "").rstrip("/")
    if not endpoint:
        sys.exit("ATAI_API_ENDPOINT is not set (put it in .env)")
    # A prod endpoint has no /agents API at all — it 404s on every path below,
    # which reads like a missing blueprint rather than the wrong host. Say so here.
    if "api.dev" not in endpoint:
        print(f"  NOTE: {endpoint} is not the dev endpoint. The /agents API is "
              f"dev-only; prod returns 404 for every agent path.")
    return endpoint


def require_key() -> str:
    load_dotenv()
    key = os.environ.get("ATAI_API_KEY")
    if not key:
        sys.exit("ATAI_API_KEY is not set (put it in .env)")
    return key


def agents_base() -> str:
    return f"{api_base()}/agents"


def request(method: str, url: str, body=None, raw: bool = False,
            retries: int = 4, allow_status: bool = False):
    """Call the API, retrying transient network failures.

    Runs are polled for up to an hour, and a DNS hiccup should not kill the
    client while the platform job carries on burning GPU unattended. Only
    *network* errors retry; an HTTP status is a real answer from the server.

    allow_status=True returns (status, body) instead of exiting, which is what
    discovery needs — a 404 is a finding, not a crash.
    """
    require_key()
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {os.environ['ATAI_API_KEY']}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                # 202 is success when starting a run. Treating only 201 as
                # success means reporting a failure while the agent runs.
                if not allow_status and resp.status not in (200, 201, 202):
                    sys.exit(f"{method} {url} unexpected status {resp.status}")
                out = payload if raw else json.loads(payload or b"null")
                return (resp.status, out) if allow_status else out
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if allow_status:
                return e.code, detail
            sys.exit(f"{method} {url} failed ({e.code}): {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries:
                if allow_status:
                    return 0, str(e)
                sys.exit(f"{method} {url} failed after {retries + 1} attempts: {e}")
            wait = 5 * (2 ** attempt)
            print(f"  network error ({e}); retrying in {wait}s [{attempt + 1}/{retries}]")
            time.sleep(wait)


def guess_content_type(path: str) -> str:
    """Infer the multipart Content-Type from the extension.

    The files API enforces a MIME allowlist on what you DECLARE, not on the
    bytes. An .mp4 sent as text/csv is rejected, and a file sent with no type is
    rejected too, so this is not cosmetic. `text/plain` for the SOP is accepted.
    """
    ctype, _ = mimetypes.guess_type(path)
    return ctype or "application/octet-stream"


def upload_file(path: str, rename: str | None = None) -> str:
    """POST a file to /v0.5/files, streamed from disk. Returns the file_id.

    The body is streamed rather than buffered: preamble + file handle +
    epilogue with an explicit Content-Length. These clips are only ~8 MB, but
    reading a video into a bytes literal doubles its resident size and there is
    no reason to.

    NOTE the returned `file_id` is the BASENAME, not the `fil_...` uid also in
    the response. It is the basename that `connectors.source[].id` wants, and
    passing the uid there fails at source resolution ~1 s into the run — after
    the pod has started.
    """
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    boundary = uuid.uuid4().hex
    filename = rename or os.path.basename(path)
    preamble = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {guess_content_type(path)}\r\n\r\n".encode(),
    ])
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    size = os.path.getsize(path)

    with open(path, "rb") as fh:
        body = _ChainedReader([preamble, fh, epilogue])
        req = urllib.request.Request(f"{api_base()}/v0.5/files", data=body,
                                     method="POST")
        req.add_header("Authorization", f"Bearer {os.environ['ATAI_API_KEY']}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(preamble) + size + len(epilogue)))
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read())["file_id"]
        except urllib.error.HTTPError as e:
            sys.exit(f"upload of {path} failed ({e.code}): "
                     f"{e.read().decode(errors='replace')}")


def upload_text(text: str, name: str) -> str:
    """Write `text` to a temp .txt and upload it. Returns the file_id.

    This is the SOP path. The blueprint's PrepareSOPNode "accumulates the SOP
    step lines into a single prepared-instruction string", so the file is
    ONE STEP PER LINE with no numbering, no blank lines and no header.
    """
    os.makedirs("data", exist_ok=True)
    path = os.path.join("data", name)
    with open(path, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return upload_file(path)


class _ChainedReader:
    """A read()-able that concatenates bytes chunks and file objects in order."""

    def __init__(self, parts):
        self._parts = list(parts)

    def read(self, amt: int = -1) -> bytes:
        out = b""
        while self._parts and (amt < 0 or len(out) < amt):
            want = -1 if amt < 0 else amt - len(out)
            head = self._parts[0]
            chunk = head[:want] if isinstance(head, (bytes, bytearray)) else head.read(want)
            if isinstance(head, (bytes, bytearray)):
                self._parts[0] = head[len(chunk):]
                if not self._parts[0]:
                    self._parts.pop(0)
            elif not chunk:
                self._parts.pop(0)
            out += chunk
        return out


def get_blueprint(key: str) -> dict:
    """Fetch a blueprint, exiting with guidance if it is not in the org."""
    status, payload = request("GET", f"{agents_base()}/blueprints/{key}",
                              allow_status=True)
    if status == 404:
        sys.exit(f"blueprint '{key}' is not registered in your org — ask an org "
                 f"admin to register it, or list what is with:\n"
                 f"  python3 inspect_bundle.py --blueprints")
    if status != 200:
        sys.exit(f"GET blueprints/{key} failed ({status}): {payload}")
    return payload


def inert_values(doc: dict, values: dict) -> list[str]:
    """Which of `values` the blueprint will silently ignore.

    A value is honoured only if it is declared in the blueprint's `values` AND
    referenced as ${values.<key>} by some node or connector. The API accepts
    anything else, stores it on the bundle, and drops it — so this is the check
    worth running BEFORE spending 15 minutes of GPU.

    It reads the blueprint document rather than hardcoding key names, so it
    keeps working after an upstream fix: a value that starts being wired flips
    from inert to honoured with no code change here.
    """
    wired = json.dumps({"nodes": doc.get("nodes"),
                        "connectors": doc.get("connectors")})
    return [k for k in values
            if k not in doc.get("values", {})
            or ("${values." + k + "}") not in wired]


# Sink formats that have actually produced output, and one that provably cannot.
#
# `tva` was republished on 2026-08-11 at 22:01:18 with its sink format changed
# from `jsonl/per-request` to `json/per-request`, for which no connector is
# registered. Every run against it dies at graph instantiation:
#
#   ERROR  instantiating graph: no connector registered for
#          format="json/per-request" kind="s3"
#   ERROR  pod.terminated  Container agent terminated: Error (exit=1)
#
# The platform validates the graph AFTER downloading and loading both models, so
# the failure costs the full cold start — 996 s observed — and leaves no results.
# Nothing in the bundle response hints at it: the bundle is `status: ready`.
#
# There is no endpoint that lists registered connectors, so this is an allowlist
# of what has run plus the one value observed to be broken. It reads the blueprint
# document, so a fixed blueprint stops warning with no code change here.
KNOWN_GOOD_SINK_FORMATS = {"jsonl/per-request"}
KNOWN_BROKEN_SINK_FORMATS = {"json/per-request"}


def check_sink(doc: dict) -> str | None:
    """Return a warning if the blueprint's sink looks un-instantiable, else None."""
    fmt = ((doc.get("connectors") or {}).get("sink") or {}).get("config", {}).get("format")
    if fmt in KNOWN_BROKEN_SINK_FORMATS:
        return (f"sink format {fmt!r} has NO REGISTERED CONNECTOR on dev. This run "
                f"will load both models (~7 min) and then die at graph "
                f"instantiation with no results.\n"
                f"    Pin a blueprint whose sink is one of "
                f"{sorted(KNOWN_GOOD_SINK_FORMATS)} — see the README.")
    if fmt not in KNOWN_GOOD_SINK_FORMATS:
        return (f"sink format {fmt!r} has not been seen to work here. If the run "
                f"dies at 'instantiating graph', this is why.")
    return None


def create_bundle(blueprint: str, name: str, values: dict) -> dict:
    """POST /agents/bundles — note the PLURAL. The singular form 404s."""
    return request("POST", f"{agents_base()}/bundles",
                   body={"blueprint": blueprint, "name": name, "values": values})


def run(bundle_id: str, video_file_id: str, sop_file_id: str) -> str:
    """Start a run with a video AND an SOP text input. Returns the agent id.

    Both inputs go in the same `connectors.source` list and MultiModalSource
    routes them by `format`: `mp4` reaches `source.video`, `txt` reaches
    `source.text`. Omit the text input and the run still starts, then the
    prompt generator waits on an instruction that never arrives.
    """
    agent = request("POST", f"{agents_base()}/bundles/{bundle_id}/run",
                    body={"connectors": {"source": [
                        {"type": "file", "id": video_file_id, "format": "mp4"},
                        {"type": "file", "id": sop_file_id, "format": "txt"},
                    ]}})
    return agent["id"]


def poll_logs(agent_id: str, timeout_s: int = 3600, interval_s: int = 20,
              quiet: bool = False) -> str:
    """Stream /logs until a terminal event. Returns 'completed'/'failed'/'timeout'.

    Two things this deliberately does NOT do:

      * It does not read the instance `status` field, which lies in both
        directions — `running` has persisted 20+ minutes after a pod terminated
        with exit=1, and `running` after a job completed.
      * It does not poll /events, which carries only "run started" and
        "dispatched to JOS". /logs is the stream the console shows: model load
        timings, per-input progress and the real error.
    """
    seen: set[str] = set()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rows = sorted(
            request("GET", f"{agents_base()}/instances/{agent_id}/logs?limit=500")
            .get("data", []),
            key=lambda r: str(r.get("created_at")))
        for r in rows:
            marker = f"{r.get('created_at')}|{r.get('event_type')}|{r.get('message')}"
            if marker in seen:
                continue
            seen.add(marker)
            if not quiet:
                print(f"  {str(r.get('created_at'))[11:19]} "
                      f"{str(r.get('level')):7} {str(r.get('event_type')):28} "
                      f"{r.get('message')}")
        if rows and str(rows[-1].get("event_type")) in TERMINAL_EVENTS:
            if any(str(r.get("level")) == "ERROR" for r in rows):
                return "failed"
            # A WARN from the parser is not cosmetic: "generation ended inside the
            # model's reasoning block (no </think>)" means the whole token budget
            # went on reasoning and NO verdicts were emitted. The job still
            # reports completed and /results still returns a record — with an
            # empty results list. Report it as its own outcome so a caller cannot
            # mistake it for a successful run.
            for r in rows:
                msg = str(r.get("message", ""))
                if str(r.get("level")) == "WARN" and "reasoning block" in msg:
                    if not quiet:
                        print(f"  PARSER WARNING: {msg}")
                    return "empty-reasoning-overflow"
            return "completed"
        time.sleep(interval_s)
    return "timeout"


def fetch_results(agent_id: str, out_path: str) -> bytes | None:
    """Fetch /results and save the first output. Returns the bytes."""
    results = request("GET", f"{agents_base()}/instances/{agent_id}/results")
    items = results.get("data") or []
    if not items:
        print("  no results (a failed run leaves none)")
        return None
    inner = items[0].get("data") or {}
    ref = inner.get("ref") or items[0].get("ref")
    if not ref:
        print("  results carried no download ref")
        return None
    # A run output's ref is a RELATIVE platform path that resolves under /v0.5
    # and needs the bearer token; an absolute ref is presigned S3 and must NOT
    # get the header. Run outputs do not expire, so this is re-fetchable later.
    if ref.startswith("http"):
        with urllib.request.urlopen(ref, timeout=300) as resp:
            payload = resp.read()
    else:
        payload = request("GET", f"{api_base()}/v0.5{ref}", raw=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    open(out_path, "wb").write(payload)
    return payload


def cancel(agent_id: str) -> None:
    """POST /cancel. Do NOT send DELETE expecting a no-op — it removes the run."""
    request("POST", f"{agents_base()}/instances/{agent_id}/cancel",
            allow_status=True)
