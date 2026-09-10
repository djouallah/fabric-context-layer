"""Publish the built context as Delta tables through duckrun - into a Fabric lakehouse of
its own, or into a local folder for the selftest.

The lakehouse is the artifact. Written into one, the context becomes a Fabric item: its SQL
analytics endpoint answers T-SQL over nodes, edges, terms and definitions, Power BI and
every other Fabric engine can read it, and the harvest of a tenant lives in that tenant.
Nothing of it stays on the machine that built it.

Where it went is recorded in `context.json` at the repo root - an address, not content. The
harvest side writes it here; the query side reads it to know what to open. It is the only
thing besides the tables themselves that the two sides share.

Views are materialised so the published copy is self-contained.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, Iterable, Optional

from common import ROOT

LOCATION = os.path.join(ROOT, "context.json")
TABLES = ("nodes", "edges", "activity", "query_usage", "query_stats", "terms",
          "definitions", "aliases", "item_usage", "meta", "flow", "measure_usage",
          "item_views")


def _source(source: Any):
    """A read-only DuckDB connection over `source`, plus whether we opened it ourselves.
    `source` is either an open connection (the in-memory build) or a path to a file."""
    if hasattr(source, "execute"):
        return source, False
    import duckdb
    return duckdb.connect(source, read_only=True), True


def publish(source: Any, target: str, tables: Iterable[str] = TABLES,
            quiet: bool = True) -> Dict[str, int]:
    """Write every published table of `source` (a connection or a .duckdb path) to `target`
    as Delta, replacing what is there. Returns {table: rows}."""
    import logging

    import duckrun

    if quiet:
        logging.getLogger("duckrun").setLevel(logging.WARNING)
    src, mine = _source(source)
    present = {r[0] for r in src.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema IN ('main', 'temp')").fetchall()}
    conn = duckrun.connect(target, read_only=False)
    counts: Dict[str, int] = {}
    try:
        for table in tables:
            if table not in present:
                continue
            frame = src.execute("SELECT * FROM " + table).fetch_arrow_table()
            conn.register("src_" + table, frame)
            conn.sql("CREATE OR REPLACE TABLE " + table + " AS SELECT * FROM src_" + table)
            counts[table] = frame.num_rows
    finally:
        if mine:
            src.close()
    return counts


def publish_lakehouse(source: Any, workspace: str, lakehouse: str,
                      tables: Iterable[str] = TABLES,
                      work: Optional[str] = None,
                      folder: Optional[str] = None) -> Dict[str, object]:
    """Ensure `lakehouse` exists in `workspace` (created schema-enabled when missing) and
    write every published table into it as `dbo.<table>`. Idempotent: rerunning replaces
    the tables. Records the address in context.json and returns it."""
    from duckrun.workspace import Workspace

    ws = Workspace(workspace)
    existing = {lh.get("displayName"): lh.get("id") for lh in ws.list_lakehouses()}
    created = lakehouse not in existing
    lh_id = ws.create_lakehouse(lakehouse, schemas=True, folder=folder)
    if folder and not created:
        place(ws, lh_id, folder)          # create_lakehouse leaves an existing one put
    counts = publish(source, ws.id + "/" + lh_id, tables)
    info = {"workspace": ws.display_name, "workspace_id": ws.id, "lakehouse": lakehouse,
            "lakehouse_id": lh_id, "path": ws.id + "/" + lh_id, "created": created,
            "published_at": dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0, tzinfo=None).isoformat() + "Z",
            "tables": counts,
            "url": "https://app.fabric.microsoft.com/groups/" + ws.id + "/lakehouses/" + lh_id}
    if work:
        info["work"] = work              # where the working copy of Files/ lives on this machine
    if folder:
        info["folder"] = folder
    save_location(info)
    return info


def place(ws, item_id: str, folder: str) -> Optional[str]:
    """Move an existing item into the workspace folder `folder`, creating the folder if it
    is not there. Idempotent, and best effort: an item that cannot be moved is still a
    working item, so a tenant without the folders API does not fail a publish."""
    from duckrun.fabric_remote import _FABRIC_API, _ensure_folder, _http_request

    try:
        folder_id = _ensure_folder(ws._token, ws.id, folder, required=True)
        if not folder_id:
            return None
        resp = _http_request("POST", _FABRIC_API + "/workspaces/" + ws.id + "/items/"
                             + item_id + "/move", token=ws._token,
                             json_body={"targetFolderId": folder_id})
        if resp.status_code not in (200, 201, 202):
            resp = _http_request("PATCH", _FABRIC_API + "/workspaces/" + ws.id + "/items/"
                                 + item_id, token=ws._token, json_body={"folderId": folder_id})
        return folder_id if resp.status_code in (200, 201, 202) else None
    except Exception:                                  # noqa: BLE001 - placement is cosmetic
        return None


# ------------------------------------------------------------------ the address

def save_location(info: Dict[str, Any], path: str = LOCATION) -> str:
    """Record where the context was published. GUIDs, not display names: `path` is the
    `<workspace-guid>/<lakehouse-guid>` shorthand duckrun expands to a OneLake URL, and it
    survives a rename of either."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(info, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    return path


def load_location(path: str = LOCATION) -> Optional[Dict[str, Any]]:
    """What the last publish wrote, or None when nothing has been published."""
    try:
        with open(path, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("path") else None
