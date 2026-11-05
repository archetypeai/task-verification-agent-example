"""The project, its copies of `tva`, and what is built from them: every id in one file.

Every script that runs TVA goes through agent(). For one setting — max_frames,
size, max_new_tokens — it finds or makes, in order:

    project  "TVA o-ring"            one per org, found by name
    copy     tva-f64-s224-t5760      `tva` with this setting's values set on it
    package  of that copy            its models resolved once
    deployment, batch or serving     of that package

and returns the setting's record. Sessions — one per clip for batch, one per
sweep for serving — are opened by the caller and recorded here with
record_session() BEFORE anything waits on them, so cleanup.py and
`sweep.py --collect` find a session whose client died.

WHY A COPY PER SETTING. A bundle carried `values`; a package, a deployment and a
batch session carry none (a batch session refuses them with a 422). The one place
left to set them is the blueprint, and a copy into a project takes
`overrides.values`. So each setting is its own copy, named after its values, and
it is reused by every later run at that setting, from any script. A plain copy
with no overrides would run at the blueprint's own defaults, which on dev
(2026-11) include max_new_tokens 8192 — the budget this example measured looping
inside <think> and returning results:[] on a hard clip (see
tva_api.DEFAULT_MAX_NEW_TOKENS).

State goes to state/lifecycle.json, for one deployment of the platform: pointing
.env at another one stops with a message to move state/ aside. A lock serialises
the scripts this example lets you run at once (iterate_sop.py), so two of them
never build two packages for one setting.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager

import tva_api as api

PROJECT = "TVA o-ring"
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE = os.path.join(STATE_DIR, "lifecycle.json")


def log(msg: str) -> None:
    print(f"  {datetime.datetime.now():%H:%M:%S} {msg}", flush=True)


@contextmanager
def locked():
    """Hold the state lock, across processes, for one read-modify-write."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_state() -> dict:
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if state.get("endpoint") and state["endpoint"] != api.api_base():
        sys.exit(f"{STATE} records {state['endpoint']}, not {api.api_base()}: "
                 f"move state/ aside to run on this one")
    state["endpoint"] = api.api_base()
    state.setdefault("settings", {})
    state.setdefault("sessions", {})
    return state


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE)


def setting_key(values: dict, source: str = "tva") -> str:
    """`tva-f64-s224-t5760`: the copy's key, and the setting's name everywhere.

    A copy of anything but the `tva` key is prefixed with the source's last six
    characters, so two sources at one setting never share a key."""
    prefix = "tva" if source == "tva" else "tva-" + re.sub(r"[^a-z0-9]", "", source.lower())[-6:]
    return (f"{prefix}-f{values['max_frames']}-s{values['size']}"
            f"-t{values['max_new_tokens']}")


def _project(state: dict) -> str:
    if "project" not in state:
        found = [p for p in api.request("GET", f"{api.api_base()}/projects?limit=1000")["data"]
                 if p["name"] == PROJECT]
        state["project"] = found[0]["id"] if found else api.request(
            "POST", f"{api.api_base()}/projects",
            body={"name": PROJECT, "description": "Task Verification on the o-ring cap assembly"})["id"]
        save_state(state)
        log(f"project {state['project']} ({PROJECT})")
    return state["project"]


def _alive(url: str) -> dict | None:
    status, obj = api.request("GET", url, allow_status=True)
    return obj if status == 200 and isinstance(obj, dict) else None


def agent(values: dict, kind: str, source: str = "tva") -> dict:
    """The setting's record, with a `ready` deployment of `kind` in it.

    Reuses whatever state/lifecycle.json holds that still answers, and rebuilds
    only what is gone: a package deleted by cleanup.py, a deployment stopped."""
    key = setting_key(values, source)
    with locked():
        state = load_state()
        project = _project(state)
        rec = state["settings"].setdefault(key, {"source": source, "values": values})

        if not rec.get("blueprint") or not _alive(f"{api.agents_base()}/blueprints/{rec['blueprint']}"):
            found = api.request("GET", f"{api.agents_base()}/blueprints?project_id={project}&key={key}")["data"]
            if found:
                rec["blueprint"] = found[0]["id"]
                log(f"reusing {key} ({rec['blueprint']})")
            else:
                bp = api.copy_blueprint(source, project, key, values, name=f"TVA {key}")
                rec["blueprint"] = bp["id"]
                log(f"copied {source} as {key} ({bp['id']}) with {json.dumps(values)}")
            rec.pop("package", None)
            save_state(state)

        pkg = _alive(api.packages_url(rec["package"])) if rec.get("package") else None
        if not pkg or pkg["status"] not in ("ready", "building"):
            pkg = api.create_package(rec["blueprint"], f"TVA {key}")
            rec["package"] = pkg["id"]
            for k in ("batch", "serving"):
                rec.pop(k, None)
            save_state(state)
            log(f"package {pkg['id']} building (models resolved once) ...")
        if pkg["status"] != "ready":
            api.package_ready(rec["package"])

        dpl = _alive(api.deployments_url(rec[kind])) if rec.get(kind) else None
        if not dpl or dpl["status"] not in ("ready", "deploying"):
            dpl = api.deploy(rec["package"], kind)
            rec[kind] = dpl["id"]
            save_state(state)
            log(f"{kind} deployment {dpl['id']}")
        if dpl["status"] != "ready":
            api.deployment_ready(rec[kind], timeout_s=1800)
        return {"key": key, **rec}


def record_session(session_id: str, **info) -> None:
    """Note a session as soon as it exists, for cleanup.py and sweep.py --collect."""
    with locked():
        state = load_state()
        state["sessions"][session_id] = {
            **info, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        save_state(state)


def forget(setting: str, kind: str) -> None:
    """Drop a deployment the caller has stopped and deleted."""
    with locked():
        state = load_state()
        state["settings"].get(setting, {}).pop(kind, None)
        save_state(state)


def sessions() -> dict:
    """Every session recorded on this deployment of the platform."""
    with locked():
        return load_state()["sessions"]
