"""Put the harvest on a schedule: ship the code, deploy a notebook, tell Fabric when to run it.

Four steps, all idempotent, all through duckrun's Workspace:

1. the code goes to `Files/code` in the context lakehouse, beside the data it produces;
2. the notebook is built from `notebook.py` as a **pure-Python** Fabric notebook - the
   metadata matters, because the wrong kernel silently gets you a Spark item;
3. it is deployed with `overwrite=True`, which is `updateDefinition`, so the item id and
   its schedule survive a redeploy;
4. the schedule is PATCHed rather than added, so redeploying does not stack triggers.

After this the laptop is optional. The notebook harvests incrementally every night and
publishes to the same lakehouse this file read the address from.
"""
from __future__ import annotations

import json
import os
import pprint
import tempfile
from typing import Dict, List, Optional

import files
import notebook as nb
from common import HERE

# What the notebook needs to run the pipeline: every module plus the two data files the
# code reads. Not the query side - ask/ never runs in there.
CODE_SUFFIXES = (".py", ".sql", ".html", ".yaml")
CODE_SKIP = ("selftest.py", "deploy.py", "notebook.py", "run.py")


def code_files(src_dir: str = HERE) -> List[str]:
    """The files that make up the harvest, as absolute paths."""
    out = []
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path) or not name.endswith(CODE_SUFFIXES):
            continue
        if name in CODE_SKIP:
            continue
        out.append(path)
    return out


def build_ipynb(config: Dict, path: str) -> str:
    """Write the refresh notebook to `path` as pure-Python ipynb.

    The config cell is a Python literal, not JSON: `json.dumps` writes `true`/`false`/`null`,
    which are three NameErrors in a notebook cell."""
    from duckrun.fabric_remote import _python_code_cell, _python_notebook

    body = pprint.pformat(config, indent=1, width=94, sort_dicts=True)
    cells = [_python_code_cell(src) for src in nb.cells(body)]
    for src in nb.cells(body):
        compile(src, "<cell>", "exec") if not src.startswith("!") else None
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(_python_notebook(cells), handle, indent=1)
    return path


def deploy(workspace: str, lakehouse: str, store: str, workspaces: List[str],
           name: str = "context_refresh", days: int = 28, stale_after_days: float = 1.0,
           profile: bool = True, wiki: bool = True, query_log: bool = False,
           daily: Optional[str] = "03:00", tz: str = "UTC", folder: Optional[str] = "context",
           run: bool = False, log=print) -> Dict[str, object]:
    """Ship the code, deploy the notebook, schedule it. Returns what it did."""
    from duckrun.workspace import Workspace

    config = {"store": store, "lakehouse": workspace + "/" + lakehouse,
              "workspaces": list(workspaces), "days": int(days),
              "stale_after_days": float(stale_after_days),
              "profile": bool(profile), "wiki": bool(wiki), "query_log": bool(query_log),
              "folder": folder}

    staged = _stage_code(store, log)
    tmp = os.path.join(tempfile.mkdtemp(prefix="ctx_nb_"), name + ".ipynb")
    build_ipynb(config, tmp)

    ws = Workspace(workspace)
    item_id = ws.deploy(tmp, overwrite=True, name=name, folder=folder)
    log("deployed notebook " + name + " (" + str(item_id) + ") to " + ws.display_name
        + (" in folder " + folder if folder else ""))

    schedule_id = None
    if daily:
        schedule_id = ws.schedule(name, daily=daily, tz=tz)
        log("scheduled daily at " + daily + " " + tz + " (" + str(schedule_id) + ")")

    status = None
    if run:
        log("running it now ...")
        status = ws.run(name)
        log("run finished: " + str(status))

    return {"notebook": name, "item_id": item_id, "workspace": ws.display_name,
            "workspace_id": ws.id, "schedule_id": schedule_id, "code_files": staged,
            "config": config, "run_status": status,
            "url": "https://app.fabric.microsoft.com/groups/" + ws.id
                   + "/synapsenotebooks/" + str(item_id)}


def _stage_code(store: str, log=print) -> int:
    """Copy the harvest's own source into Files/code, as a diff like everything else."""
    staging = tempfile.mkdtemp(prefix="ctx_code_")
    folder = os.path.join(staging, "code")
    os.makedirs(folder, exist_ok=True)
    for path in code_files():
        with open(path, "rb") as src, open(os.path.join(folder, os.path.basename(path)),
                                           "wb") as dst:
            dst.write(src.read())
    out = files.push(store, staging, ["code"], full=True, log=lambda _m: None)
    n = out.get("code", {}).get("sent", 0)
    log("shipped " + str(n) + " code files to Files/code")
    return n
