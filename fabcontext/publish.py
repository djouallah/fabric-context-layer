"""Write the built context out as Delta tables - into a Fabric lakehouse, or into a local
folder when there is no tenant.

The lakehouse is the artifact. Written into one, the context becomes a Fabric item: its SQL
analytics endpoint answers T-SQL over nodes, edges, terms and definitions, every other Fabric
engine can read it, and the harvest of a tenant lives in that tenant. Nothing of it stays on
the machine that built it.

The address of that lakehouse is the whole contract with whatever answers questions later.
`harvest()` returns it; nothing is written to disk to be found again.

Views are materialised, so the published copy stands on its own.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, Optional, Tuple

from ._fabric import Workspace, delta, onelake

TABLES = ("nodes", "edges", "activity", "query_usage", "query_stats", "terms",
          "definitions", "aliases", "item_usage", "meta", "flow", "measure_usage",
          "item_views")

SCHEMA = "dbo"


def publish(con, store, tables: Iterable[str] = TABLES) -> Dict[str, int]:
    """Write every published table of `con` into `store`, replacing what is there.

    Each table streams straight from DuckDB to delta-rs over an Arrow C stream - it is never
    materialised in between, so publishing a large graph costs no more memory than a scan.
    Returns {table: rows}.
    """
    present = {row[0] for row in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema IN ('main', 'temp')").fetchall()}
    root, options = store.tables_root, store.storage_options
    counts: Dict[str, int] = {}
    for table in tables:
        if table not in present:
            continue
        rows = con.execute("SELECT count(*) FROM " + table).fetchone()[0]
        delta.write_table(delta.table_url(root, SCHEMA, table),
                          con.sql("SELECT * FROM " + table), options)
        counts[table] = int(rows)
    return counts


def open_lakehouse(workspace: str, lakehouse: str,
                   folder: Optional[str] = None) -> Tuple[Any, bool]:
    """(store, created) for the lakehouse the context lives in, making it if it is not there.

    Idempotent, and that is the whole create-or-update story: an existing lakehouse comes back
    untouched, and `created` tells the caller whether there is a previous harvest in `Files/`
    worth pulling down before this one starts.
    """
    ws = Workspace(workspace)
    existing = {lh.get("displayName") for lh in ws.list_lakehouses()}
    created = lakehouse not in existing
    lh_id = ws.create_lakehouse(lakehouse, schemas=True, folder=folder)
    if folder and not created:
        ws.move_item(lh_id, folder)          # create_lakehouse leaves an existing one put
    store = onelake.OneLakeStore(ws.id, lh_id)
    store.workspace = ws.display_name
    store.lakehouse = lakehouse
    return store, created


def publish_lakehouse(con, workspace: str, lakehouse: str,
                      tables: Iterable[str] = TABLES,
                      folder: Optional[str] = None) -> Dict[str, Any]:
    """Open the lakehouse and write the context into it, in one call. Returns where it went."""
    store, created = open_lakehouse(workspace, lakehouse, folder)
    ws_id, lh_id = store.workspace_id, store.item_id
    counts = publish(con, store, tables)
    return {"workspace": store.workspace, "workspace_id": ws_id, "lakehouse": lakehouse,
            "lakehouse_id": lh_id, "created": created,
            "url": store.tables_root,
            "portal": onelake.portal_url(ws_id, lh_id),
            "published_at": dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0, tzinfo=None).isoformat() + "Z",
            "tables": counts, "store": store}
