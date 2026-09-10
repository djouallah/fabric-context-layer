"""Step 2b: lakehouse columns, statistics and values -> raw/<ws>/profiles/<lakehouse-id>.json

The harvest lists tables; it does not open them. This step does, as cheaply as it can:
columns, types, row counts and per-column min/max come from the Delta log alone (no data
is read), and distinct values are scanned only for string columns of tables a semantic
model binds to, capped at fifty values and gated by an approximate count first. `build` then attaches the result to the
lakehouse_table nodes and copies it onto the model columns that read them, so an agent
can translate "NSW" into a filter literal without a live call.

Selection is what the graph says is used: tables that are the target of a sources_from,
reads or feeds edge. --all profiles every table (log only), --no-values skips the scan.

Named profiling.py rather than profile.py so it cannot shadow the standard library module.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import duckdb

import fabric_api as api
from common import read_json, write_json

RAW = ""          # the working raw/ folder; run.py sets it from common.workdir()

REFERENCED = ("sources_from", "reads", "feeds")
VALUE_CAP = 50            # distinct values kept per column
VALUE_SCAN_MAX_ROWS = 500_000_000  # above this a value scan is skipped, the log stats stay
VALUE_LEN = 100           # characters kept per value
STRING_TYPES = ("string", "utf8", "varchar", "large_string", "char", "text")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


# ---------------------------------------------------------------- selection

def select_tables(con, stores: List[str] = (), all_tables: bool = False) -> List[Dict]:
    """[{lakehouse_id, lakehouse, workspace, workspace_id, schema, table, model_bound,
    n_refs, external}] for every table worth profiling."""
    rows = con.execute("""
        SELECT t.item_id                                   AS lakehouse_id,
               l.name                                      AS lakehouse,
               t.workspace,
               coalesce(w.item_id, json_extract_string(l.attrs, '$.workspace_id')) AS workspace_id,
               coalesce(json_extract_string(t.attrs, '$.schema'), 'dbo')         AS schema,
               t.name                                      AS tbl,
               bool_or(e.rel = 'sources_from')             AS model_bound,
               count(e.src)                                AS n_refs,
               coalesce(TRY_CAST(json_extract_string(l.attrs, '$.external') AS BOOLEAN), false) AS external
          FROM nodes t
          JOIN nodes l ON l.id = t.parent_id AND l.kind = 'lakehouse'
          LEFT JOIN nodes w ON w.kind = 'workspace' AND w.name = t.workspace
          LEFT JOIN edges e ON e.dst = t.id AND e.rel IN ('sources_from', 'reads', 'feeds')
         WHERE t.kind = 'lakehouse_table'
         GROUP BY ALL
         ORDER BY l.name, schema, tbl""").fetchall()
    wanted = {s.lower() for s in stores}
    out = []
    for lh_id, lh, ws, ws_id, schema, tbl, bound, n_refs, external in rows:
        if wanted and lh_id.lower() not in wanted and str(lh).lower() not in wanted:
            continue
        if not all_tables and not n_refs:
            continue
        if not ws_id:
            continue                                   # nowhere to read it from
        out.append({"lakehouse_id": lh_id, "lakehouse": lh, "workspace": ws,
                    "workspace_id": ws_id, "schema": schema, "table": tbl,
                    "model_bound": bool(bound), "n_refs": int(n_refs or 0),
                    "external": bool(external)})
    return out


# ---------------------------------------------------------------- delta log

def _table_urls(ws_id: str, lh_id: str, schema: str, table: str) -> List[str]:
    base = "abfss://" + ws_id + "@onelake.dfs.fabric.microsoft.com/" + lh_id + "/Tables/"
    urls = [base + schema + "/" + table]
    if schema.lower() == "dbo":
        urls.append(base + table)                      # a lakehouse without schemas
    return urls


def open_logs(urls: List[str], so: Dict[str, str]) -> List[Optional[Any]]:
    """One DeltaTable (or None) per url, opened concurrently - the log replay is the slow
    part of profiling and it is all network latency."""
    from dbt.adapters.duckrun.engine import open_delta_tables
    return open_delta_tables([(u, so) for u in urls])


def _arrow_fields(table: Any):
    """The table's columns as Arrow fields, whichever spelling this deltalake has."""
    schema = table.schema()
    for name in ("to_arrow", "to_pyarrow"):
        if hasattr(schema, name):
            return list(getattr(schema, name)())
    return [f for f in schema.fields]                  # delta-rs fields: .name and .type


def log_profile(table: Any, url: str) -> Optional[Dict]:
    """Columns, row count and per-column min/max/null share from an opened Delta log."""
    if table is None:
        return None
    import pyarrow as pa

    columns = [{"name": f.name, "type": str(f.type)} for f in _arrow_fields(table)]
    adds = table.get_add_actions(flatten=True)
    frame = pa.Table.from_batches([adds]) if isinstance(adds, pa.RecordBatch) else adds
    cur = duckdb.connect()
    cur.register("add_actions", frame)
    have = {c[0] for c in cur.execute("SELECT * FROM add_actions LIMIT 0").description}
    n_files, n_rows = cur.execute(
        "SELECT count(*), coalesce(sum(num_records), 0)::BIGINT FROM add_actions").fetchone()
    stats: Dict[str, Dict] = {}
    for col in columns:
        name = col["name"]
        mn, mx, nc = "min." + name, "max." + name, "null_count." + name
        if mn not in have or mx not in have:
            continue
        q = lambda s: '"' + s.replace('"', '""') + '"'      # noqa: E731
        try:
            lo, hi, nulls = cur.execute(
                "SELECT min(" + q(mn) + "), max(" + q(mx) + "), "
                + ("coalesce(sum(" + q(nc) + "), 0)::BIGINT" if nc in have else "0")
                + " FROM add_actions").fetchone()
        except Exception:                              # noqa: BLE001 - odd type, skip column
            continue
        if lo is None and hi is None:
            continue
        stats[name] = {"min": _jsonable(lo), "max": _jsonable(hi),
                       "null_frac": (int(nulls) / n_rows) if n_rows else 0.0}
    cur.close()
    return {"columns": columns, "n_rows": int(n_rows or 0), "n_files": int(n_files or 0),
            "version": table.version(), "partition_columns": table.metadata().partition_columns,
            "stats": stats, "url": url}


# ---------------------------------------------------------------- values

class _Store:
    """One duckrun session per lakehouse, opened lazily; the scan side of profiling.

    Distinct values are what lets an agent write a filter literal without guessing, and they
    are the only part of profiling that reads data. Two queries per table keep it cheap:

    1. `approx_count_distinct` over every string column at once. HyperLogLog, one pass, no
       sort - on a table of tens of millions of rows an exact COUNT(DISTINCT) per column is
       the whole cost, and the number is only ever used as a gate.
    2. For the columns whose estimate is near or under the cap, `SELECT DISTINCT ... LIMIT
       cap + 1`. That listing IS the exact answer: cap + 1 rows back means "more than cap
       distinct" and the column is dropped; fewer means the row count is the exact
       n_distinct.

    So a column that comes back with `values` has an exact `n_distinct`; a column with no
    `values` has the HyperLogLog estimate. That is the only rule a reader needs.
    """

    # An estimate this far above the cap is trusted to mean "far too many". Nearer than
    # that and the exact listing decides, because HyperLogLog is only approximately right.
    GATE = 2.0

    def __init__(self, workspace_id: str, lakehouse_id: str, name: str):
        self.path = workspace_id + "/" + lakehouse_id
        self.name = name
        self._session = None

    def _con(self):
        if self._session is None:
            import logging

            import duckrun

            logging.getLogger("duckrun").setLevel(logging.WARNING)
            self._session = duckrun.connect(self.path, read_only=True)
        return self._session.con

    def close(self) -> None:
        try:
            if self._session is not None:
                self._session.close()
        except Exception:                              # noqa: BLE001 - nothing to do about it
            pass

    def values(self, schema: str, table: str, columns: List[Dict]) -> Tuple[Dict, Dict]:
        """({column: n_distinct}, {column: [values]}) for the string columns."""
        strings = [c["name"] for c in columns
                   if any(t in str(c.get("type", "")).lower() for t in STRING_TYPES)]
        if not strings:
            return {}, {}
        ref = _q(schema) + "." + _q(table)
        con = self._con()
        row = con.execute("SELECT " + ", ".join(
            "approx_count_distinct(" + _q(c) + ")" for c in strings) + " FROM " + ref).fetchone()
        ndv = {c: int(n or 0) for c, n in zip(strings, row)}

        values: Dict[str, List] = {}
        for col, n in list(ndv.items()):
            if n == 0 or n > VALUE_CAP * self.GATE:
                continue                               # estimate stands, no listing
            out = con.execute("SELECT DISTINCT " + _q(col) + " FROM " + ref
                              + " WHERE " + _q(col) + " IS NOT NULL ORDER BY 1 LIMIT "
                              + str(VALUE_CAP + 1)).fetchall()
            if len(out) > VALUE_CAP:                   # over the cap, and now known exactly
                continue
            ndv[col] = len(out)                        # the listing is the exact count
            values[col] = [str(r[0])[:VALUE_LEN] for r in out]
        return ndv, values


def _q(name: str) -> str:
    """A double-quoted SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


# ---------------------------------------------------------------- driver

def _raw_folders() -> Dict[str, str]:
    """{workspace id: raw folder}; external stores land under raw/external."""
    out: Dict[str, str] = {}
    if not os.path.isdir(RAW):
        return out
    for name in os.listdir(RAW):
        ws = read_json(os.path.join(RAW, name, "workspace.json"), None)
        if ws and ws.get("id"):
            out[ws["id"]] = os.path.join(RAW, name)
    return out


def run(con, stores: List[str] = (), all_tables: bool = False, values: bool = True,
        dry_run: bool = False) -> Dict[str, int]:
    """`con` is a connection over the built context - the in-memory one `graph.build`
    returned, or the one `graph.open_published` pulled back out of the lakehouse. This step
    only reads it, to choose which tables are worth profiling."""
    targets = select_tables(con, stores, all_tables)
    _log("profile: " + str(len(targets)) + " tables selected"
         + (" (--all)" if all_tables else " (referenced by a model, notebook or pipeline)"))
    if dry_run:
        for t in targets:
            _log("  " + t["lakehouse"] + " " + t["schema"] + "." + t["table"]
                 + ("  [model-bound, values]" if t["model_bound"] and values else "")
                 + ("  [external]" if t["external"] else ""))
        return {"selected": len(targets)}
    if not targets:
        return {"selected": 0}

    so = {"bearer_token": api.onelake_token()}
    folders = _raw_folders()
    by_store: Dict[Tuple[str, str], List[Dict]] = {}
    for t in targets:
        by_store.setdefault((t["workspace_id"], t["lakehouse_id"]), []).append(t)

    done = failed = scanned = 0
    for (ws_id, lh_id), tables in by_store.items():
        folder = folders.get(ws_id) or os.path.join(RAW, "external")
        path = os.path.join(folder, "profiles", lh_id + ".json")
        profile = read_json(path, {}) or {}
        session = _Store(ws_id, lh_id, tables[0]["lakehouse"])
        _log("  " + tables[0]["lakehouse"] + " (" + str(len(tables)) + " tables)")
        # open every table's log at once, then retry the schema-less path for the misses
        urls = [_table_urls(ws_id, lh_id, t["schema"], t["table"]) for t in tables]
        opened = open_logs([u[0] for u in urls], so)
        retry = [i for i, o in enumerate(opened) if o is None and len(urls[i]) > 1]
        if retry:
            again = open_logs([urls[i][1] for i in retry], so)
            for i, o in zip(retry, again):
                opened[i] = o
                if o is not None:
                    urls[i] = [urls[i][1]]
        for t, table_obj, table_urls in zip(tables, opened, urls):
            key = t["schema"] + "." + t["table"]
            entry = None
            try:
                entry = log_profile(table_obj, table_urls[0])
            except Exception as exc:                   # noqa: BLE001 - stats are best effort
                _log("    [warn] " + key + ": " + str(exc)[:120])
            if not entry:
                failed += 1
                _log("    " + key + ": no Delta log found")
                continue
            if values and t["model_bound"] and entry["n_rows"] > VALUE_SCAN_MAX_ROWS:
                _log("    " + key + ": " + format(entry["n_rows"], ",")
                     + " rows, value scan skipped (cap " + format(VALUE_SCAN_MAX_ROWS, ",") + ")")
            elif values and t["model_bound"]:
                try:
                    ndv, vals = session.values(t["schema"], t["table"], entry["columns"])
                    entry["n_distinct"], entry["values"] = ndv, vals
                    scanned += 1
                except Exception as exc:               # noqa: BLE001 - stats still useful
                    _log("    [warn] values for " + key + ": " + str(exc)[:120])
            entry["profiled_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat()
            profile[key] = entry
            done += 1
            _log("    " + key + ": " + str(len(entry["columns"])) + " columns, "
                 + format(entry["n_rows"], ",") + " rows"
                 + (", values for " + str(len(entry.get("values") or {})) + " columns"
                    if entry.get("values") else ""))
        session.close()
        write_json(path, profile)
        _log("  wrote " + path)
    _log("profile: " + str(done) + " profiled, " + str(scanned) + " value-scanned, "
         + str(failed) + " failed. Run build again to publish them.")
    return {"selected": len(targets), "profiled": done, "scanned": scanned, "failed": failed}
