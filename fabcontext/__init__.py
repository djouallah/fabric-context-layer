"""One call: name a workspace, get back a lakehouse holding the ranked context of it.

    %pip install fabcontext
    from fabcontext import harvest
    url = harvest("My Workspace")

The first call creates the lakehouse; every later call updates it. Nothing else to configure
and nothing kept on the machine that ran it - the lakehouse is the only state, and the URL it
returns is the whole contract with whatever answers questions from it later.

It is built for the Fabric Python runtime and asks for nothing that runtime does not already
have, so the install replaces no native library and needs no kernel restart.
"""
from __future__ import annotations

import os
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Union

__version__ = "0.1.0"

__all__ = ["harvest", "build_and_publish", "open_context", "__version__"]

DEFAULT_LAKEHOUSE = "context_layer"


def _steps(log):
    """A per-step timing table. A step that dies leaves the ones before it published, and the
    table at the end says which failed and why - a half-built context that reports honestly
    beats one that vanishes with a traceback."""
    rows = []

    def step(name, fn, *args, **kwargs):
        start = time.time()
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:                    # noqa: BLE001 - recorded, then re-raised
            rows.append((name, round(time.time() - start, 1), "FAILED"))
            log("[FAILED] " + name + "  " + exc.__class__.__name__ + ": " + str(exc)[:400])
            raise
        rows.append((name, round(time.time() - start, 1), "ok"))
        log("[ok] " + name + "  " + str(round(time.time() - start, 1)) + "s  "
            + str(out)[:200])
        return out

    return step, rows


def _work_dirs(work: str):
    raw = os.path.join(work, "raw")
    build = os.path.join(work, "build")
    wiki_dir = os.path.join(work, "wiki")
    for folder in (raw, build, wiki_dir):
        os.makedirs(folder, exist_ok=True)
    return raw, build, wiki_dir, os.path.join(work, "graph.html")


def _target(to: Optional[str], first_workspace: str):
    """(workspace, lakehouse) for `to`, or the default: a lakehouse called `context_layer` in
    the first workspace named, so the context lives beside what it describes."""
    if not to:
        return first_workspace, DEFAULT_LAKEHOUSE
    workspace, _sep, lakehouse = to.partition("/")
    if not workspace or not lakehouse:
        raise ValueError("`to` is <workspace>/<lakehouse>; got " + repr(to))
    return workspace, lakehouse


def build_and_publish(work: str, store, *, profile: bool = True, values: bool = True,
                      wiki: bool = True, aliases: Optional[Dict[str, List[str]]] = None,
                      push: bool = True, log=print) -> Dict:
    """Everything downstream of the fetch: parse `work/raw`, rank it, publish, render, push.

    Split out from `harvest` because it needs no tenant - a folder of harvested JSON and a
    local store are enough to run the whole second half, which is how the offline suite
    exercises the real publish rather than a mock.
    """
    from . import files, graph, parse, profiling, publish

    step, rows = _steps(log)
    raw, build, wiki_dir, graph_html = _work_dirs(work)

    def _build():
        parse.build(raw, build)
        return graph.build(build, aliases)

    con, counts = step("build", _build)
    try:
        if profile:
            step("profile", profiling.run, con, raw, values=values)
            con.close()
            con, counts = step("build (with profiles)", _build)
        published = step("publish", publish.publish, con, store)
        if wiki:
            from . import viz
            from . import wiki as wiki_mod
            step("wiki", wiki_mod.render, con, wiki_dir)
            step("graph.html", viz.render, con, graph_html)
    finally:
        con.close()
    if push:
        step("push files", files.push, store, work, files.ITEMS, False, lambda _m: None)
    return {"counts": counts, "tables": published, "steps": rows}


def harvest(workspaces: Union[str, Sequence[str]], to: Optional[str] = None, *,
            days: int = 28, query_log: bool = False, refresh: bool = False,
            profile: bool = True, values: bool = True, wiki: bool = True,
            folder: Optional[str] = "context", stale_after_days: float = 1.0,
            aliases: Optional[Dict[str, List[str]]] = None, work: Optional[str] = None,
            log=print) -> str:
    """Harvest `workspaces` into a lakehouse and return its URL.

    `workspaces` is a name, a GUID, or a list of either. `to` names the lakehouse to publish
    into as `<workspace>/<lakehouse>`; by default it is `context_layer` in the first workspace
    given. `aliases` merges spellings the word lists cannot, as {term_id: [spelling, ...]}.

    **Create or update.** The lakehouse is made if it is not there and reused if it is, and an
    existing one has its previous `Files/raw` pulled down first - which is what makes a second
    run incremental rather than a full re-read of the tenant.

    `query_log` additionally reads each workspace's monitoring Eventhouse for the DAX that
    actually ran, the one signal that says a measure was evaluated rather than merely written
    into a report. Off by default: monitoring bills against the capacity, and a workspace
    without it is skipped.

    Two levels of permission, and it says which it got rather than failing: **contributor** on
    the target workspace to create the lakehouse, and **Fabric admin** for the Scanner API and
    the audit log. Without admin the graph still builds, but loses endorsement, cross-workspace
    lineage and usage.
    """
    from . import common, fetch, files, publish

    names = [workspaces] if isinstance(workspaces, str) else list(workspaces)
    if not names:
        raise ValueError("name at least one workspace to harvest")
    workspace, lakehouse = _target(to, names[0])
    common.set_aliases(aliases)

    step, _rows = _steps(log)
    store, created = step("open lakehouse", publish.open_lakehouse,
                          workspace, lakehouse, folder)

    own_work = work is None
    work = work or tempfile.mkdtemp(prefix="fabcontext_")
    raw, _build, _wiki, _html = _work_dirs(work)
    try:
        if not created:
            # The previous harvest. Its absence is not an error - a lakehouse someone made by
            # hand, or a run that died before its first push, simply starts from scratch.
            step("stage previous raw/", files.pull, store, work, ["raw"], lambda _m: None)
        step("harvest", fetch.harvest, raw, names, days, refresh, True, True,
             query_log, stale_after_days)
        out = build_and_publish(work, store, profile=profile, values=values, wiki=wiki,
                                aliases=aliases, log=log)
    finally:
        if own_work:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

    log("")
    log("published " + str(sum(out["tables"].values())) + " rows across "
        + str(len(out["tables"])) + " tables")
    log(store.tables_root)
    return store.tables_root


def open_context(url: str, storage_options: Optional[Dict[str, str]] = None):
    """A read-only DuckDB connection over a published context.

    `url` is what `harvest` returned. This is the harvest side reading back what it wrote -
    for looking at a context without rebuilding it.
    """
    from ._fabric import auth, onelake
    from . import graph

    class _Store:
        tables_root = url
        storage_options = None

    store = _Store()
    if storage_options is not None:
        store.storage_options = storage_options
    elif url.startswith("abfss://"):
        store.storage_options = onelake.storage_options(auth.onelake_token())
    return graph.read_published(store)
