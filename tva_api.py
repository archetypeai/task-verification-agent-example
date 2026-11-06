#!/usr/bin/env python3
"""Minimal stdlib-only client for the Archetype AI Agent API, for TVA.

Shared by run_tva_example.py, sweep.py, sweep_serve.py, iterate_sop.py and
cleanup.py. Auth and endpoint come from the environment, with a local .env
loaded if present:

    ATAI_API_KEY=<API key>
    ATAI_API_ENDPOINT=https://platform.u1.archetypeai.app/api

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

The Agent Lifecycle 2.0: a copy of `tva` in a project, with this run's values
set on it, is packaged (its models resolved once), the package is deployed as
`batch` or `serving`, and work runs in sessions on the deployment. A batch
session is one run over one video and its SOP; a serving session answers
`/query` until it is cancelled. tva_project.py keeps the ids.
"""
from __future__ import annotations

import datetime
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# A session's terminal statuses.
TERMINAL = {"completed", "failed", "cancelled"}


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # A quoted value would otherwise keep its quotes: `"https://…"` is not a URL.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def api_base() -> str:
    load_dotenv()
    endpoint = os.environ.get("ATAI_API_ENDPOINT", "").rstrip("/")
    if not endpoint:
        sys.exit("ATAI_API_ENDPOINT is not set (put it in .env)")
    return endpoint


def files_root() -> str:
    """Where the files API and run-output refs resolve.

    The platform.* endpoints (`https://platform[.dev].u1.archetypeai.app/api`)
    serve files at `<endpoint>/files`; the legacy api.* hosts served them at
    `<endpoint>/v0.5/files`. /agents is at `<endpoint>/agents` on both.
    """
    endpoint = api_base()
    return endpoint if endpoint.endswith("/api") else f"{endpoint}/v0.5"


def require_key() -> str:
    load_dotenv()
    key = os.environ.get("ATAI_API_KEY")
    if not key:
        sys.exit("ATAI_API_KEY is not set (put it in .env)")
    return key


def agents_base() -> str:
    return f"{api_base()}/agents"


def request(method: str, url: str, body=None, raw: bool = False,
            retries: int = 4, allow_status: bool = False, timeout: int = 120):
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def run_suffix() -> str:
    """`<UTC timestamp>-<4 hex>`, computed ONCE per run and shared by its inputs.

    `file_id` IS the basename, so an org shares one flat namespace. Two people running
    this example at the same time would otherwise upload to the same names — and a run
    pins its inputs at input-resolution time, so the second upload destroys the first's
    queued run. It surfaces minutes later as `S3 object not found`, inside a run that
    already started, with `job.completed` on the job.

    Each part earns its place:
      the original stem  recognisable in an org-wide file list
      the UTC timestamp  sorts and greps; tells you which upload was yours and when
      4 hex              the part that actually guarantees it — two people starting in
                         the same second is precisely the case being fixed

    The video and the SOP MUST share this suffix: the pipeline pairs them by matching
    stems, so a per-file suffix would break the pairing it exists to protect.
    """
    return (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid.uuid4().hex[:4])


def remote_bytes(file_id: str) -> bytes | None:
    """The object currently stored under `file_id`, or None if absent."""
    status, payload = request("GET", f"{files_root()}/files/download/{file_id}",
                              raw=True, allow_status=True)
    return payload if status == 200 and isinstance(payload, bytes) else None


def upload_file(path: str, rename: str | None = None, verify: bool = True) -> str:
    """POST a file to the files API, streamed from disk. Returns the file_id.

    IDEMPOTENT: if an object with the same name and the same bytes is already on
    the platform, this SKIPS the upload. That is not an optimisation — it prevents
    a data-loss bug that cost three runs here.

    `file_id` IS the basename, so re-uploading replaces the stored object. A run
    pins the object at input-resolution time, so replacing it while that run is
    still queued destroys its input, and the run fails minutes later with
    `S3 object not found`. Observed exactly:

        05:51:25  run A resolves inputs, pinning SOP object f7307e1d-…
        05:53:45  run B uploads oring.txt again -> new object replaces A's
        later     run A executes -> its object is gone -> S3 object not found

    Skipping the redundant upload keeps readable, stable filenames AND makes
    concurrent runs over the same inputs safe. When the bytes genuinely differ the
    upload must happen, so that case warns instead.

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
    filename = rename or os.path.basename(path)
    # Skip the upload when the identical object is already there — see docstring.
    # Read the local file only when it is small enough to compare cheaply; a
    # multi-gigabyte input is not worth buffering to save one request.
    if os.path.getsize(path) < 64 * 1024 * 1024:
        with open(path, "rb") as fh:
            local = fh.read()
        existing = remote_bytes(filename)
        if existing == local:
            print(f"  {filename}: identical object already on the platform — "
                  f"skipping upload (re-uploading would break any run already "
                  f"referencing it)")
            return filename
        if existing is not None:
            print(f"  WARNING: {filename} exists with DIFFERENT bytes. Replacing "
                  f"it breaks any run already referencing it — those fail with "
                  f"'S3 object not found' minutes later.")
    boundary = uuid.uuid4().hex
    preamble = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {guess_content_type(path)}\r\n\r\n".encode(),
    ])
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    size = os.path.getsize(path)

    with open(path, "rb") as fh:
        body = _ChainedReader([preamble, fh, epilogue])
        req = urllib.request.Request(f"{files_root()}/files", data=body,
                                     method="POST")
        req.add_header("Authorization", f"Bearer {os.environ['ATAI_API_KEY']}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(preamble) + size + len(epilogue)))
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"upload of {path} failed ({e.code}): "
                     f"{e.read().decode(errors='replace')}")
    # A 2xx is not enough. The response carries its own `is_valid` flag, and an
    # upload that reports valid=false here — or returns no file_id — would
    # otherwise be discovered ~13 minutes later, inside the run, as
    # `S3 object not found`. That happened once on this dataset: one clip 404'd on
    # /files/download afterwards while its siblings were fine, so the object never
    # persisted even though the client had a file_id in hand.
    if not out.get("is_valid", True) or not out.get("file_id"):
        sys.exit(f"upload of {path} was rejected by the files API: "
                 f"{json.dumps(out)}\n"
                 f"  Nothing downstream will work — the run would fail with "
                 f"'S3 object not found' about 13 minutes in.")
    file_id = out["file_id"]
    if verify:
        # `is_valid: true` and a file_id are NOT sufficient. Twice on this dataset
        # an upload reported success and the S3 object was simply absent — once a
        # video, once the SOP text — each discovered only inside a run, ~13 min
        # later, as `S3 object not found`. Fetching the file back is the only
        # check that actually confirms it landed, and it costs one request.
        status, _ = request("GET", f"{files_root()}/files/download/{file_id}",
                            raw=True, allow_status=True)
        if status != 200:
            sys.exit(f"upload of {path} reported success but the object is not "
                     f"retrievable (GET /files/download/{file_id} -> "
                     f"{status}).\n"
                     f"  This has happened twice here; re-uploading has fixed it. "
                     f"Retry rather than starting a run, which would fail ~13 min "
                     f"in with 'S3 object not found'.")
    return file_id


def upload_text(text: str, name: str, verify: bool = True) -> str:
    """Write `text` to a temp .txt and upload it. Returns the file_id.

    This is the SOP path. The blueprint's PrepareSOPNode "accumulates the SOP
    step lines into a single prepared-instruction string", so the file is
    ONE STEP PER LINE with no numbering, no blank lines and no header.
    """
    os.makedirs("data", exist_ok=True)
    path = os.path.join("data", name)
    with open(path, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return upload_file(path, verify=verify)


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
        sys.exit(f"blueprint '{key}' is not visible to this API key — list what "
                 f"is with GET {agents_base()}/blueprints")
    if status != 200:
        sys.exit(f"GET blueprints/{key} failed ({status}): {payload}")
    return payload


def inert_values(doc: dict, values: dict) -> list[str]:
    """Which of `values` the blueprint will silently ignore.

    A value is honoured only if it is declared in the blueprint's `values` AND
    referenced as ${values.<key>} by some node or connector. A copy refuses an
    undeclared key with a 400, but a declared value that nothing references is
    accepted and has no effect — so this is the check worth running BEFORE
    spending 15 minutes of GPU.

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
# Nothing in the bundle response hinted at it: the bundle was `status: ready`.
#
# There is no endpoint that lists registered connectors, so this is an allowlist
# of what has run plus the one value observed to be broken. It reads the blueprint
# document, so a fixed blueprint stops warning with no code change here.
# Both work as of 2026-08-12 16:42 UTC. `json/per-request` was un-instantiable for
# ~18 hours (three consecutive canonical versions) and has since been fixed — a run
# on the current canonical blueprint instantiated cleanly and wrote its output.
#
# The two differ in PACKAGING, not content:
#   jsonl/per-request -> one .jsonl file, one line per request
#   json/per-request  -> one NUMBERED .json file per request (…_0001.json)
# So with several inputs in one run, `json` yields SEVERAL result entries. See
# fetch_results(), which reads only the first and warns when there are more.
KNOWN_GOOD_SINK_FORMATS = {"jsonl/per-request", "json/per-request"}
KNOWN_BROKEN_SINK_FORMATS: set[str] = set()   # kept: the check is the regression test


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


# The model's total context in this deployment. NOT exposed as a blueprint value, and
# the prompt and the generation budget must both fit inside it:
#
#   estimated prompt 2368 tokens + 8191 reserved for generation exceeds
#   max_seq_len=8192; use fewer frames or raise max_seq_len / lower max_new_tokens
#
# `max_new_tokens` is RESERVED UP FRONT, so it is not a cap on what may be
# generated — it is subtracted from the window before generation starts. The check
# fires at fusion time, AFTER ~7 minutes of model loading.
# CHANGED UNDER US. Measured at 8192 on 2026-08-11: a run at max_new_tokens 8191
# with a 2368-token prompt was rejected with "prompt 2368 tokens + 8191 reserved for
# generation exceeds max_seq_len=8192". On 2026-08-12 a run with prompt 2376 +
# max_new_tokens 6928 = 9304 completed cleanly, with no warning — so the window is
# now larger than 9304 and this constant was refusing valid configurations.
#
# The node schema's own ceiling is 262144 (256K), which matches the context the
# design doc advertises, so the served window may now be anywhere in (9304, 262144].
# It is NOT exposed as a blueprint value, so it cannot be read — only bracketed by
# runs. Treated as a floor rather than a limit: the preflight now warns instead of
# refusing, because a stale constant that blocks good work is worse than one that
# lets a bad run through.
#
# Raised again on 2026-08-12: the paired 12-clip sweep ran max_frames=64 with
# sop/oring-numbered.txt (prompt ~2376) and max_new_tokens=8192 — total 10568 — and
# 8 of the 12 runs completed and returned parsed verdicts. The 4 that produced no
# verdicts failed inside the model's reasoning block, NOT at fusion, so they are
# not evidence against the window either.
#
# Raised again, and by a lot, on 2026-08-13. The BLUEPRINT'S OWN DEFAULT for
# max_new_tokens is 16384 — printed in every run's preflight as "blueprint default:
# 16384" — which was a standing hint that the window is far larger than our
# bracketing suggested. A run at max_frames=64 with max_new_tokens=16384 (prompt
# ~2370, total ~18754) cleared graph instantiation AND fusion, and generated until
# it exhausted the budget. So the window is >= 18754, and the earlier 8192/9304/
# 10568 values were all just the largest we had happened to try.
#
# Note what this does NOT mean: a bigger window is not a bigger answer. That same
# run returned results:[] at 16384 exactly as it had at 8192. See
# DEFAULT_MAX_NEW_TOKENS.
MAX_SEQ_LEN_KNOWN_GOOD = 18754       # observed to work (oring-question @ 64 frames, 16384)
MAX_SEQ_LEN = 262144                 # the schema ceiling; the real limit is unknown

# The DEFAULT output budget — deliberately NOT the ceiling.
#
# max_usable_new_tokens() returns the largest budget the window allows, and using
# that as the default was wrong twice over:
#
#   * The pathology here is REPETITION. The model emits the verdict block over and
#     over, so a bigger budget is more room to loop, not more room to reason.
#     Measured on 1_fail_2_pass_3_fail_A, one clip, two budgets:
#         8192 -> looped inside <think>, never closed  -> 0 rows
#         5760 -> closed <think>, looped in the answer -> 21 rows, verdicts CORRECT
#   * A COMPUTED default drifts. Raising MAX_SEQ_LEN_KNOWN_GOOD from 9304 to 10568
#     silently moved the default from 6864 to 8128 — into the region that returns
#     nothing — as a side effect of recording an unrelated fact about the window.
#     A fixed number cannot do that.
#
# Every answer we have on a defect-bearing clip came from 5120-5760. Callers who
# want the ceiling can still ask for it explicitly.
DEFAULT_MAX_NEW_TOKENS = 5760

# Vision token arithmetic for the preprocessor defaults (patch 16, merge 2,
# temporal patch 2, size 224): each temporal step is (224/16/2)^2 = 49 tokens, and
# frames are paired into steps. Calibrated against the platform's own figure of
# 2368 tokens for max_frames=64, which implies ~800 tokens of system + SOP text.
VISION_TOKENS_PER_STEP = 49

# Text overhead, split so it can adapt to the SOP actually in use.
#
# The platform reported a 2368-token prompt for max_frames=64, size=224 with the
# 3-step baseline SOP. Visual accounts for 1568 of that, leaving 800 for the system
# message, the chat template AND the SOP. Folding the SOP into a constant was the
# first version of this and it is wrong for any other SOP: the strict variant is
# ~94 tokens longer, which alone exceeds the 64-token safety margin and would make
# the estimate low enough to be rejected at fusion time — after ~7 min of loading.
#
# So the constant below is the SOP-FREE part, and the SOP is measured separately.
BASELINE_SOP_TOKENS = 69          # sop/oring.txt, 276 chars at ~4 chars/token
SYSTEM_TOKEN_OVERHEAD = 800 - BASELINE_SOP_TOKENS
CHARS_PER_TOKEN = 4               # crude, and deliberately so — see estimate below


def estimate_sop_tokens(sop_text_or_path: str | None) -> int:
    """Rough token count for the SOP. Errs HIGH, because erring low costs a run."""
    if not sop_text_or_path:
        return BASELINE_SOP_TOKENS
    text = sop_text_or_path
    if os.path.exists(sop_text_or_path):
        with open(sop_text_or_path) as fh:
            # Count only what is actually UPLOADED. load_sop() drops blank lines and
            # # comments, so counting the raw file charges the prompt for commentary
            # the model never sees — sop/oring-sealant.txt has a 9-line rationale
            # block that inflated the estimate by ~125 tokens and made the budget
            # warning fire on a run that was nowhere near the ceiling.
            text = "".join(l for l in fh
                           if l.strip() and not l.lstrip().startswith("#"))
    # Round up: an underestimate is rejected at fusion time after the cold start,
    # while an overestimate merely leaves a few generation tokens unused.
    return -(-len(text) // CHARS_PER_TOKEN) + 8


def estimate_prompt_tokens(max_frames: int, size: int = 224,
                           sop: str | None = None) -> int:
    """Estimate the prompt's token cost, to check it against MAX_SEQ_LEN."""
    per_step = (size // 16 // 2) ** 2
    steps = max(1, max_frames // 2)
    return steps * per_step + SYSTEM_TOKEN_OVERHEAD + estimate_sop_tokens(sop)


def max_usable_new_tokens(max_frames: int, size: int = 224,
                          margin: int = 64, sop: str | None = None) -> int:
    """The largest `max_new_tokens` that fits alongside the prompt.

    Worth computing rather than guessing. The nine runs that produced verdicts in
    this example asked for 8192 — more than the window — and the engine gave them
    whatever remained after the prompt: 8192 - 2368 = 5824 tokens. A later
    "safe-looking" default of 5120 therefore gave the model LESS reasoning room
    than the runs that worked, for no reason. Leaving headroom unused is a real
    cost here, because reasoning length is what fails first on the hardest clips.

    `margin` guards against the prompt estimate being slightly low (a longer SOP,
    a different system message) — being rejected at fusion time costs the whole
    cold start, so a few dozen tokens of slack is cheap insurance.
    """
    return max(256, MAX_SEQ_LEN_KNOWN_GOOD
               - estimate_prompt_tokens(max_frames, size, sop) - margin)


def check_token_budget(max_frames: int, max_new_tokens: int,
                       size: int = 224, sop: str | None = None) -> str | None:
    """Return a message if prompt + generation cannot fit in the context window.

    Worth doing client-side because the platform checks it at fusion time — after
    whisper and newton-fusion have both loaded, so a violation costs the full
    ~7-minute cold start and produces no output.
    """
    prompt = estimate_prompt_tokens(max_frames, size, sop)
    total = prompt + max_new_tokens
    if total <= MAX_SEQ_LEN_KNOWN_GOOD:
        return None
    if total > MAX_SEQ_LEN:
        return (f"prompt (~{prompt}) + max_new_tokens ({max_new_tokens}) = {total} "
                f"exceeds the node schema's ceiling of {MAX_SEQ_LEN}. This is "
                f"rejected at blueprint resolution.")
    # Between the known-good floor and the schema ceiling the served window is
    # unknown, so this is a caution rather than a refusal.
    return (f"prompt (~{prompt}) + max_new_tokens ({max_new_tokens}) = {total} "
            f"exceeds {MAX_SEQ_LEN_KNOWN_GOOD}, the largest total OBSERVED to work.\n"
            f"    The served max_seq_len is not exposed and has been raised at least "
            f"once, so this may be fine.\n"
            f"    If it is not, the run dies at fusion time after ~7 min with "
            f"'exceeds max_seq_len=<N>' — and <N> is worth reporting.")


def packages_url(package_id: str = "") -> str:
    """A package pins one blueprint and the models it resolves to. It carries no
    values: it runs its blueprint exactly as stored, so max_frames, size and
    max_new_tokens are set on the blueprint copy it is built from."""
    base = f"{agents_base()}/packages"
    return f"{base}/{package_id}" if package_id else base


def deployments_url(deployment_id: str = "") -> str:
    base = f"{agents_base()}/deployments"
    return f"{base}/{deployment_id}" if deployment_id else base


def sessions_url(session_id: str = "") -> str:
    base = f"{agents_base()}/sessions"
    return f"{base}/{session_id}" if session_id else base


def _wait_for(url: str, status: str, waiting: str, timeout_s: int, every_s: int = 5) -> dict:
    obj = request("GET", url)
    t0 = time.time()
    while obj["status"] == waiting:
        if time.time() - t0 > timeout_s:
            sys.exit(f"{obj['id']} still {waiting} after {timeout_s} s")
        time.sleep(every_s)
        obj = request("GET", url)
    if obj["status"] != status:
        sys.exit(f"{obj['id']} is {obj['status']}, not {status}: {obj.get('error')}")
    return obj


def copy_blueprint(source: str, project_id: str, key: str, values: dict,
                   name: str | None = None) -> dict:
    """POST /agents/blueprints/{source}/copy with `values` set in the copy.

    This is where a bundle's `values` went. A copy takes `overrides.values`, each
    of which must be one the blueprint declares (an undeclared key is a 400, unlike
    a bundle, which stored it and ignored it). `tva` fits nothing, so any of its
    values may be overridden. The copy lands in `project_id` under `key`; a key
    already in use there is a 409.
    """
    body = {"project_id": project_id, "blueprint_key": key,
            "overrides": {"values": values}}
    if name:
        body["name"] = name
    return request("POST", f"{agents_base()}/blueprints/{source}/copy", body=body)


def create_package(blueprint_id: str, name: str) -> dict:
    """Package one blueprint for the platform; it starts `building`.

    Record its id before package_ready(), so a build that fails or is
    interrupted can still be deleted."""
    return request("POST", packages_url(), body={
        "blueprint_ids": [blueprint_id], "target": {"kind": "platform"}, "name": name[:128]})


def package_ready(package_id: str, timeout_s: int = 900) -> dict:
    return _wait_for(packages_url(package_id), "ready", "building", timeout_s)


def deploy(package_id: str, kind: str) -> dict:
    """Deploy a ready package as `batch` or `serving`; it starts `deploying`.

    A batch deployment is ready at once; a serving one is placed on a GPU worker
    first. Record its id before deployment_ready()."""
    return request("POST", deployments_url(), body={"package_id": package_id, "kind": kind})


def deployment_ready(deployment_id: str, timeout_s: int = 900) -> dict:
    return _wait_for(deployments_url(deployment_id), "ready", "deploying", timeout_s)


def open_session(deployment_id: str, video_file_id: str | None = None,
                 sop_file_id: str | None = None) -> dict:
    """A batch session over one video AND its SOP, or a serving session with neither.

    Both inputs go in the same `connectors.source` list, exactly as a bundle run
    took them, and MultiModalSource routes them by `format`: `mp4` reaches
    `source.video`, `txt` reaches `source.text`. The order does not matter. Omit
    the text input and the session still starts, then the prompt generator waits
    on an instruction that never arrives.
    """
    body = {"deployment_id": deployment_id}
    if video_file_id:
        body["connectors"] = {"source": [
            {"type": "file", "id": video_file_id, "format": "mp4"},
            {"type": "file", "id": sop_file_id, "format": "txt"},
        ]}
    return request("POST", sessions_url(), body=body)


def wait_running(session_id: str, timeout_s: int = 1800) -> dict:
    """A serving session takes queries once it is `running`."""
    return _wait_for(sessions_url(session_id), "running", "pending", timeout_s, every_s=10)


def local_time(stamp) -> str:
    """An event's UTC `created_at` as local HH:MM:SS, like the script's own lines."""
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) \
            .astimezone().strftime("%H:%M:%S")
    except ValueError:
        return str(stamp)[11:19]


def poll_session(session_id: str, timeout_s: int = 3600, interval_s: int = 20,
                 quiet: bool = False) -> str:
    """Poll a batch session until it ends, printing its events as they appear.

    Returns the final status (`completed`, `failed`, `cancelled`), or `timeout`.

    A session has no /logs, only /events: what the platform records about the
    session (opened, dispatched, failed with a reason), not the model-load timings
    and parser messages a bundle run's /logs carried. So the parser's warning that
    generation ended inside the reasoning block is no longer visible here. A run
    that hit it still ends `completed`, with `results: []` in its output: count
    the verdicts (see empty_output()).
    """
    seen: set[str] = set()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ses = request("GET", sessions_url(session_id))
        events = request("GET", f"{sessions_url(session_id)}/events?limit=100").get("data", [])
        for ev in reversed(events):                    # newest first
            marker = f"{ev.get('created_at')}|{ev.get('message')}"
            if marker in seen:
                continue
            seen.add(marker)
            if not quiet:
                print(f"  {local_time(ev.get('created_at'))} "
                      f"{str(ev.get('level', 'info')):7} {ev.get('message')}")
        if ses["status"] in TERMINAL:
            if ses.get("error") and not quiet:
                print(f"  {session_id}: {ses['error']}")
            return ses["status"]
        time.sleep(interval_s)
    return "timeout"


def empty_output(payload: bytes | None) -> bool:
    """True when an output holds a record but no verdicts: `results: []`.

    What a run looks like when the whole token budget went on reasoning. The
    session still ends `completed`, so this is the only place it shows."""
    if not payload:
        return False
    recs = [json.loads(l) for l in payload.decode(errors="replace").splitlines() if l.strip()]
    return bool(recs) and not any(r.get("results") for r in recs)


def fetch_results(session_id: str, out_path: str) -> bytes | None:
    """Fetch the session's /results and save the first output. Returns the bytes."""
    results = request("GET", f"{sessions_url(session_id)}/results")
    items = results.get("data") or []
    if not items:
        print("  no results (a failed session leaves none)")
        return None
    if len(items) > 1:
        # `json/per-request` writes one numbered file PER REQUEST, so a session with
        # several requests returns several entries. Reading only the first would
        # silently drop the rest.
        print(f"  WARNING: {len(items)} result files returned; this saves only the "
              f"first. With json/per-request each request gets its own numbered "
              f"file.")
    inner = items[-1].get("data") or {}           # newest first: the last is the first written
    ref = inner.get("ref")
    if not ref:
        print("  results carried no download ref")
        return None
    # An absolute ref is presigned and must NOT get the bearer header. A session's
    # relative ref is a root path, `/api/files/<id>/content`, not one under the
    # endpoint, and needs the token.
    if ref.startswith("http"):
        with urllib.request.urlopen(ref, timeout=300) as resp:
            payload = resp.read()
    else:
        endpoint = api_base()
        root = endpoint.removesuffix("/api") if ref.startswith("/api/") else files_root()
        payload = request("GET", f"{root}{ref}", raw=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    open(out_path, "wb").write(payload)
    return payload


def cancel_session(session_id: str) -> None:
    """POST /cancel. Do NOT send DELETE expecting a no-op — it removes the session."""
    request("POST", f"{sessions_url(session_id)}/cancel", body={}, allow_status=True)


def end_session(session_id: str) -> None:
    """Cancel a session that is still going, then delete it."""
    status, ses = request("GET", sessions_url(session_id), allow_status=True)
    if status == 200 and ses.get("status") not in TERMINAL:
        cancel_session(session_id)
    request("DELETE", sessions_url(session_id), raw=True, allow_status=True)


def end_deployment(deployment_id: str) -> None:
    """Stop a deployment that is still up, then delete it."""
    status, dpl = request("GET", deployments_url(deployment_id), allow_status=True)
    if status == 200 and dpl.get("status") not in ("stopped", "failed"):
        request("POST", f"{deployments_url(deployment_id)}/stop", body={}, allow_status=True)
    request("DELETE", deployments_url(deployment_id), raw=True, allow_status=True)
