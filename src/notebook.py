"""The nightly refresh, as notebook cells.

Kept here as plain strings rather than inside .ipynb JSON so the code stays readable,
diffable and lint-able. `deploy.py` wraps these in a pure-Python Fabric notebook.

Running inside Fabric changes two things and nothing else:

- **Auth is free.** duckrun resolves notebookutils before anything else, so no az login,
  no tokens in the notebook, no secrets anywhere.
- **OneLake is local.** Measured on the first run: staging all 564 files and 873 MB of
  `Files/raw` took 30 seconds, against about twenty minutes for the same pull from a
  laptop. That is why this can be a plain stage - work - push back rather than anything
  clever.

What makes the run cheap night after night is the harvest, not this file: a definition
refetches only when its lastUpdatedDate moves, and the audit log is one file per UTC day
with only today and yesterday refetched. A run touches one day of events and whatever
actually changed.

CONFIG is filled in at deploy time.
"""
from __future__ import annotations

# Cell 1. Alone, exactly as written. The Fabric runtime preinstalls older duckdb and
# deltalake; both are native extensions, so pip replaces the files on disk while the
# running interpreter keeps the old binaries loaded. Nothing may import them before the
# restart, and everything in memory is lost at it, so this cell holds nothing else and
# every later cell imports for itself.
INSTALL = '''!pip install -q duckrun --upgrade
notebookutils.session.restartPython()'''

# Cell 2. Written by deploy.py; every tenant-specific string in the notebook is here.
CONFIG = '''CONFIG = {config}'''

# Cell 3. Stage the code and the previous harvest onto the notebook's own disk.
STAGE = '''import os
import shutil
import sys
import time

t0 = time.time()
STEPS = []


def step(name, fn, *args, **kwargs):
    """Run one step, time it, record it. A step that dies leaves the ones before it
    published, and the table at the end says which failed and why."""
    start = time.time()
    try:
        out = fn(*args, **kwargs)
        STEPS.append((name, round(time.time() - start, 1), "ok"))
        print("[ok] " + name + "  " + str(round(time.time() - start, 1)) + "s  "
              + str(out)[:200], flush=True)
        return out
    except Exception as exc:
        STEPS.append((name, round(time.time() - start, 1), "FAILED"))
        print("[FAILED] " + name + "  " + exc.__class__.__name__ + ": " + str(exc)[:400],
              flush=True)
        raise


WORK = "/tmp/context-work"
CODE = os.path.join(WORK, "code")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(CODE, exist_ok=True)

import duckrun

session = duckrun.connect(CONFIG["store"], read_only=True)

# The code ships in the lakehouse beside the data it produces: this notebook is a runner,
# not where the pipeline lives.
step("stage code", session.download, "code", CODE, overwrite=True)
sys.path.insert(0, CODE)

import files as filesmod            # only importable once the code is staged

step("stage raw", filesmod.pull, CONFIG["store"], WORK, ["raw"], lambda _m: None)'''

# Cell 4. The pipeline, in the order run.py's `all` uses.
RUN = '''import graph
import harvest
import parse
import profiling
import publish
import viz
import wiki

RAW = os.path.join(WORK, "raw")
BUILD = os.path.join(WORK, "build")
WIKI = os.path.join(WORK, "wiki")
GRAPH_HTML = os.path.join(WORK, "graph.html")
for d in (RAW, BUILD, WIKI):
    os.makedirs(d, exist_ok=True)

harvest.RAW = RAW
profiling.RAW = RAW
publish.LOCATION = os.path.join(WORK, "context.json")
ws, _sep, lh = CONFIG["lakehouse"].partition("/")


def _harvest():
    out = harvest.harvest(CONFIG["workspaces"], days=CONFIG["days"],
                          query_log=CONFIG.get("query_log", False),
                          stale_after_days=CONFIG["stale_after_days"])
    return {"workspaces": len(out)}


def _build():
    parse.build(RAW, BUILD)
    con, counts = graph.build(BUILD)
    try:
        publish.publish_lakehouse(con, ws, lh, work=WORK, folder=CONFIG.get("folder"))
    finally:
        con.close()
    return {"nodes": counts.get("nodes"), "edges": counts.get("edges"),
            "terms": counts.get("terms")}


step("harvest", _harvest)
step("build + publish", _build)

if CONFIG["profile"]:
    con = graph.open_published(CONFIG["store"])
    try:
        step("profile", profiling.run, con, values=True)
    finally:
        con.close()
    step("build + publish (profiles)", _build)

if CONFIG["wiki"]:
    con = graph.open_published(CONFIG["store"])
    try:
        step("wiki", lambda: wiki.render(con, WIKI))
        step("graph.html", lambda: viz.render(con, GRAPH_HTML))
    finally:
        con.close()'''

# Cell 5. Send back what changed, then say what happened.
PUSH = '''step("push files", filesmod.push, CONFIG["store"], WORK,
     ["raw", "build", "wiki", "graph.html", "context.json"], False, lambda _m: None)

print("")
print("step                          secs  status")
for name, secs, status in STEPS:
    print(name.ljust(28) + str(secs).rjust(7) + "  " + status)
print("total " + str(round(time.time() - t0, 1)) + "s")

failed = [s[0] for s in STEPS if s[2] != "ok"]
if failed:
    raise SystemExit("failed: " + ", ".join(failed))'''


def cells(config_repr: str):
    """The notebook's cells, in order, with the config baked in."""
    return [INSTALL, CONFIG.format(config=config_repr), STAGE, RUN, PUSH]
