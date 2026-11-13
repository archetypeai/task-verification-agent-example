"""`--background` for long-running scripts: relaunch under nohup, keep the Mac awake, return.

The relaunched run survives closing the terminal (nohup), and on macOS `caffeinate -i -w`
keeps the Mac from idle-sleeping until it ends. Output goes to a log file. Stdlib only.
Adapted from the LARCO quickstart's prep/background.py.

    ap = argparse.ArgumentParser(...)
    add_background_flag(ap, default_log="logs/sweep_serve.log")
    args = ap.parse_args()
    maybe_detach(args)          # returns only in the foreground run

caffeinate watches the run (`-w <pid>`), so the pid printed is the Python process and
`kill <pid>` reaches it, and SIGTERM is raised as KeyboardInterrupt, so `kill` takes the
same cleanup path as Ctrl-C: a sweep killed without cleanup leaves a run, or a serving
deployment, holding a GPU.
"""
import os
import shutil
import signal
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def add_background_flag(ap, default_log):
    ap.add_argument("--background", nargs="?", const=default_log, metavar="LOG",
                    help=f"run detached under nohup (+ caffeinate on macOS), output to LOG (default {default_log})")


def _strip_flag(argv):
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--background":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            skip = nxt is not None and not nxt.startswith("-")   # --background LOG
            continue
        if a.startswith("--background="):
            continue
        out.append(a)
    return out


def _interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def maybe_detach(args):
    """With --background, start the same command detached and exit; otherwise make `kill` act like Ctrl-C."""
    if not getattr(args, "background", None):
        signal.signal(signal.SIGTERM, _interrupt)
        return
    log = args.background if os.path.isabs(args.background) else os.path.join(ROOT, args.background)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = ["nohup", sys.executable, "-u", os.path.abspath(sys.argv[0]), *_strip_flag(sys.argv[1:])]
    with open(log, "ab") as fh:
        fh.write(f"\n=== {' '.join(cmd)}\n".encode())
        fh.flush()
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=ROOT, start_new_session=True)
    awake = shutil.which("caffeinate")
    if awake:
        subprocess.Popen([awake, "-i", "-w", str(proc.pid)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    rel = os.path.relpath(log, ROOT) if log.startswith(ROOT + os.sep) else log
    print(f"running in the background (pid {proc.pid}{', caffeinate keeps the Mac awake' if awake else ''})")
    print(f"  follow:  tail -f {rel}")
    print(f"  stop:    kill {proc.pid}      # cleans up like Ctrl-C")
    sys.exit(0)
