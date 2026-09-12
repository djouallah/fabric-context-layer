"""Delta Lake in and out, through delta-rs alone.

Three things worth knowing about why it is shaped this way:

- **Writing streams.** `write_deltalake` accepts anything exporting an Arrow C stream, and a
  DuckDB relation is one, so a table goes from the in-memory graph to Delta without ever
  being materialised as an Arrow table in between.
- **Reading uses DataFusion, not `delta_scan`.** DuckDB's `delta` and `azure` extensions are
  not bundled with the Fabric runtime, so `delta_scan` would fetch them over the network in
  every notebook session. `QueryBuilder` ships inside the deltalake wheel and hands back a
  `RecordBatchReader`, which DuckDB consumes through the same Arrow capsule. No extension,
  no download, no pyarrow.
- **Opening logs is latency, not work.** Replaying a Delta log over OneLake is a handful of
  round trips, so a batch of tables is opened concurrently and the results stay positional.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Opening one Delta log is pure network wait; more threads than this buys nothing and starts
# to look like a burst to OneLake.
MAX_OPEN_WORKERS = 16


def table_url(root: str, schema: str, table: str) -> str:
    """Where one table lives under a Tables root. A local root stays a local path so the
    offline test exercises the real write, not a mock."""
    if "://" in root:
        return root.rstrip("/") + "/" + schema + "/" + table
    return os.path.join(root, schema, table)


def write_table(url: str, source: Any, storage_options: Optional[Dict[str, str]] = None) -> None:
    """Replace whatever is at `url` with `source`, schema and all.

    `source` is normally a DuckDB relation. `schema_mode="overwrite"` matters: the published
    tables gain columns as the graph gains attributes, and without it a new column would fail
    against the previous version's schema.
    """
    from deltalake import write_deltalake

    write_deltalake(url, source, mode="overwrite", schema_mode="overwrite",
                    storage_options=storage_options or None)


def open_table(url: str, storage_options: Optional[Dict[str, str]] = None):
    """The `DeltaTable` at `url`, or None when there is no Delta log there. A missing table is
    an ordinary outcome when probing for one, not an error."""
    from deltalake import DeltaTable

    try:
        return DeltaTable(url, storage_options=storage_options or None)
    except Exception:                               # noqa: BLE001 - absent or unreadable
        return None


def open_tables(targets: Sequence[Tuple[str, Optional[Dict[str, str]]]]) -> List[Optional[Any]]:
    """One `DeltaTable` (or None) per target, opened concurrently.

    The result is **positional**: callers index it against the input to retry a different URL
    shape for the ones that came back None.
    """
    if not targets:
        return []
    workers = min(MAX_OPEN_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: open_table(t[0], t[1]), targets))


def query(dt, sql: str, name: str = "t"):
    """Run `sql` against one Delta table and return a `RecordBatchReader`.

    The dialect is DataFusion's, not DuckDB's - `approx_distinct`, not
    `approx_count_distinct`.
    """
    from deltalake import QueryBuilder

    return QueryBuilder().register(name, dt).execute(sql)


def query_rows(dt, sql: str, name: str = "t") -> List[tuple]:
    """`query`, materialised as a list of tuples. For the small results - a distinct-value
    listing, a row of cardinality estimates - where streaming buys nothing."""
    import duckdb

    con = duckdb.connect()
    try:
        con.register("_q", query(dt, sql, name))
        return con.execute("SELECT * FROM _q").fetchall()
    finally:
        con.close()


def read_into(con, name: str, url: str,
              storage_options: Optional[Dict[str, str]] = None) -> bool:
    """Copy the Delta table at `url` into `con` as `name`. False when it is not there, so an
    optional table simply does not arrive rather than failing the read."""
    dt = open_table(url, storage_options)
    if dt is None:
        return False
    # Registered by name rather than left to DuckDB's replacement scan, which would resolve
    # the reader out of this frame's locals - true, but not something to rely on.
    handle = "_reader_" + name
    con.register(handle, query(dt, "SELECT * FROM t"))
    try:
        con.execute("CREATE OR REPLACE TABLE " + name + " AS SELECT * FROM " + handle)
    finally:
        con.unregister(handle)
    return True


def arrow_fields(dt) -> List[Any]:
    """A table's columns as Arrow fields, whichever spelling this deltalake version has. The
    probe is deliberate: this is the one place the versions disagree."""
    schema = dt.schema()
    for attr in ("to_arrow", "to_pyarrow"):
        if hasattr(schema, attr):
            return list(getattr(schema, attr)())
    return [f for f in schema.fields]               # delta-rs fields carry .name and .type


def add_actions(dt):
    """The Delta log's add actions as something DuckDB can register.

    `get_add_actions` hands back an arro3 `RecordBatch`, which exports an Arrow C *array* but
    not a *stream*, and DuckDB only accepts the latter. Wrapping it in a one-batch table is
    the whole fix.
    """
    adds = dt.get_add_actions(flatten=True)
    if hasattr(adds, "__arrow_c_stream__"):
        return adds
    from arro3.core import Table

    return Table.from_batches([adds])
