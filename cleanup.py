#!/usr/bin/env python3
"""Clean up: delete the sessions, deployments and packages this example left on the platform.

    python3 cleanup.py            # list what would be deleted, and what stays
    python3 cleanup.py --yes      # delete it

It starts from the packages recorded in state/lifecycle.json, one per setting
(tva-f64-s224-t5760 and so on). For each it lists the package's deployments and
their sessions, so a session or deployment a script never got to record is found
too. Sessions go first, then deployments, then the package: each holds the one
after it. A session still going is cancelled and a deployment still up is
stopped before it is deleted, and an id already gone counts as done.

A serving deployment holds a GPU worker until it is stopped, so run this after a
`sweep_serve.py --keep`, or after one that was killed before it could clean up.
Outputs already downloaded are not touched; a session's results go with it, so
run `sweep.py --collect` first if a sweep was interrupted.

What stays: the project and its copies of `tva`, one per setting. The next run
at a setting packages its copy again.
"""
from __future__ import annotations

import argparse
import sys

import tva_api as api
import tva_project as project

URLS = {"sessions": api.sessions_url, "deployments": api.deployments_url,
        "packages": api.packages_url}


def listing(kind: str, **query) -> list[str]:
    q = "&".join(f"{k}={v}" for k, v in query.items())
    return [r["id"] for r in api.list_all(f"{URLS[kind]()}?{q}")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="delete; without it, only list")
    args = ap.parse_args()

    with project.locked():
        state = project.load_state()
    targets = []                                   # (setting, kind, id), each before what it holds
    for key, rec in state["settings"].items():
        pkg = rec.get("package")
        if not pkg:
            continue
        code, _ = api.request("GET", api.packages_url(pkg), allow_status=True)
        if code == 404:
            continue
        deployments = listing("deployments", package_id=pkg)
        targets += [(key, "sessions", s) for d in deployments for s in listing("sessions", deployment_id=d)]
        targets += [(key, "deployments", d) for d in deployments]
        targets += [(key, "packages", pkg)]

    for key in state["settings"]:
        mine = [t for t in targets if t[0] == key]
        if mine:
            print(f"{key}: " + ", ".join(f"{sum(t[1] == kind for t in mine)} {kind}" for kind in URLS)
                  + " to delete")
    if not targets:
        print("nothing recorded is still on the platform")
    if state.get("project"):
        print(f"stays: project {state['project']} ({project.PROJECT}) and its copies "
              f"of tva ({', '.join(state['settings']) or 'none'})")
    if not args.yes or not targets:
        if targets:
            print("\nnothing deleted: rerun with --yes")
        return 0

    stuck, gone = [], set()
    for key, kind, obj in targets:
        code, info = api.request("GET", URLS[kind](obj), allow_status=True)
        if code == 404:
            gone.add(obj)
            continue
        if kind == "sessions" and info.get("status") not in api.TERMINAL:
            api.request("POST", f"{URLS[kind](obj)}/cancel", body={}, allow_status=True, idempotent=True)
        if kind == "deployments" and info.get("status") not in ("stopped", "failed"):
            api.request("POST", f"{URLS[kind](obj)}/stop", body={}, allow_status=True, idempotent=True)
        code, body = api.request("DELETE", URLS[kind](obj), raw=True, allow_status=True)
        if 200 <= code < 300 or code == 404:
            gone.add(obj)
        else:
            # keep going: one object that will not delete must not strand the rest
            stuck.append((kind, obj, f"{code}: {str(body)[:200]}"))

    with project.locked():
        state = project.load_state()
        for rec in state["settings"].values():
            for k in ("package", "batch", "serving"):
                if rec.get(k) in gone:
                    rec.pop(k)
        state["sessions"] = {s: x for s, x in state["sessions"].items() if s not in gone}
        project.save_state(state)
    print(f"deleted {len(gone)} of {len(targets)}")

    if stuck:
        print(f"\n{len(stuck)} object(s) would not delete:")
        for kind, obj, why in stuck:
            print(f"  {kind}/{obj}: {why}")
        print("a session holds its deployment, and a deployment its package: "
              "delete what holds them, then rerun")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
