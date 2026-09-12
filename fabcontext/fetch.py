"""Step 1: pull everything out of Fabric into raw/ as files.

Nothing is interpreted here - the goal is that a re-run is cheap and that parsing can be
rewritten without touching the tenant again. Every fetch is skipped when its file already
exists, unless --refresh or the item's lastUpdatedDate moved.

Layout:
    raw/scanner.json                       one scan covering every requested workspace
    raw/activity/YYYY-MM-DD.json           one file per UTC day (tenant-wide)
    raw/<ws-slug>/querylog/manifest.json   which eventhouse was read, over what window
    raw/<ws-slug>/querylog/YYYY-MM-DD.json QueryEnd rows, one file per UTC day
    raw/<ws-slug>/workspace.json           id, displayName, capacity
    raw/<ws-slug>/items.json               every item with its type
    raw/<ws-slug>/admin_items.json         lastUpdatedDate + creator, all item types
    raw/<ws-slug>/manifest.json            what was fetched, in what format, what failed
    raw/<ws-slug>/definitions/<Type>/<item-id>/{item.json, ...definition parts}
    raw/<ws-slug>/lakehouses/<id>.json     properties incl. the SQL endpoint id
    raw/<ws-slug>/lakehouses/<id>.tables.json
    raw/<ws-slug>/warehouses/<id>.json     + .tables.json
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import shutil
import time
from typing import Dict, List, Optional

from . import api
from ._fabric import Workspace
from .common import iso, page_slug, read_json, write_json

# Item types whose definition we ask for. Everything else is inventory only.
DEFINABLE = tuple(api.ITEM_ENDPOINT)


def _utcnow() -> dt.datetime:
    """UTC now, naive - every timestamp the harvest writes is naive UTC, and the audit
    log is keyed by UTC day."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _safe_part_path(base: str, rel: str) -> str:
    """A definition part's path joined under base, refusing anything that escapes it."""
    parts = rel.replace("\\", "/").split("/")
    if os.path.isabs(rel) or ".." in parts:
        raise ValueError("refusing definition part path " + repr(rel))
    return os.path.join(base, *parts)


def harvest(raw: str, workspaces: List[str], days: int = 28, refresh: bool = False,
            scanner: bool = True, activity: bool = True, query_log: bool = False,
            stale_after_days: float = 1.0) -> Dict[str, str]:
    """Harvest each workspace into `raw`. Returns {workspace name: raw folder}.

    `query_log` reads each workspace's monitoring Eventhouse for the DAX that actually ran;
    it is off by default because workspace monitoring bills against the capacity, and a
    workspace without it is simply skipped.

    Definitions are cached on the item's lastUpdatedDate, so they refetch when they change.
    The scanner result and the store table lists have no such signal - they are cached on
    the file simply existing, which under a schedule would freeze them forever. They are
    refetched once they are older than `stale_after_days`."""
    ftoken = api.fabric_token()
    folders: Dict[str, str] = {}
    ws_ids: List[str] = []

    for name in workspaces:
        ws = Workspace(name)
        display = ws.display_name
        folder = os.path.join(raw, page_slug(display, ws.id))
        os.makedirs(folder, exist_ok=True)
        folders[display] = folder
        ws_ids.append(ws.id)
        _log("workspace " + display + " (" + ws.id + ") -> " + os.path.relpath(folder, raw))
        _harvest_workspace(ws.id, display, folder, ftoken, refresh, stale_after_days)
        if query_log:
            _harvest_query_log(ws.id, display, folder, ftoken, days, refresh)

    if scanner:
        _harvest_scanner(raw, ws_ids, refresh, stale_after_days)
    if activity:
        _harvest_activity(raw, days, refresh)
    return folders


def _stale(path: str, days: float) -> bool:
    """Whether a cached file is missing or older than `days`. A cache with no freshness
    signal of its own needs one, or a schedule reuses the first run's answer forever."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return True
    return age > days * 86400.0


def _harvest_workspace(ws_id: str, display: str, folder: str, ftoken: str,
                       refresh: bool, stale_after_days: float = 1.0) -> None:
    write_json(os.path.join(folder, "workspace.json"),
               dict(api.workspace_info(ws_id, ftoken), displayName=display, id=ws_id))

    items = api.workspace_items(ws_id, ftoken)
    write_json(os.path.join(folder, "items.json"), items)
    _log("  " + str(len(items)) + " items")

    admin: List[Dict] = []
    try:
        admin = api.admin_items(ws_id, ftoken)
        write_json(os.path.join(folder, "admin_items.json"), admin)
    except Exception as exc:                          # noqa: BLE001 - admin is optional
        _log("  [warn] admin/items unavailable (" + type(exc).__name__ + "); "
             "modified dates will come from the scanner only")
    admin_dates = {row.get("id"): iso(row.get("lastUpdatedDate")) for row in admin}

    manifest = read_json(os.path.join(folder, "manifest.json"), {}) or {}
    fetched = skipped = failed = 0

    for item in items:
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in DEFINABLE or not item_id:
            continue
        target = os.path.join(folder, "definitions", item_type, item_id)
        prev = manifest.get(item_id) or {}
        changed = admin_dates.get(item_id) and admin_dates[item_id] != prev.get("modified")
        if prev.get("ok") and os.path.isdir(target) and not refresh and not changed:
            skipped += 1
            continue
        record = {"type": item_type, "name": item.get("displayName"),
                  "modified": admin_dates.get(item_id), "ok": False,
                  "format": None, "error": None}
        try:
            fmt, parts = api.get_definition(ws_id, item_type, item_id, ftoken)
            if not parts:
                record["error"] = "no definition returned"
            else:
                if os.path.isdir(target):
                    shutil.rmtree(target)
                os.makedirs(target, exist_ok=True)
                for part in parts:
                    dest = _safe_part_path(target, part["path"])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as fh:
                        fh.write(base64.b64decode(part["payload"]))
                write_json(os.path.join(target, "item.json"),
                           {"id": item_id, "type": item_type,
                            "displayName": item.get("displayName"),
                            "description": item.get("description"),
                            "workspaceId": ws_id, "format": fmt})
                record.update(ok=True, format=fmt)
                fetched += 1
        except Exception as exc:                      # noqa: BLE001 - one item must not stop the run
            record["error"] = type(exc).__name__ + ": " + str(exc)[:200]
            failed += 1
        manifest[item_id] = record

    write_json(os.path.join(folder, "manifest.json"), manifest)
    _log("  definitions: " + str(fetched) + " fetched, " + str(skipped) + " cached, "
         + str(failed) + " failed")

    _harvest_stores(ws_id, folder, items, ftoken, refresh, stale_after_days)


def _harvest_stores(ws_id: str, folder: str, items: List[Dict], ftoken: str,
                    refresh: bool, stale_after_days: float = 1.0) -> None:
    """Lakehouse and warehouse table lists - the bottom of the graph, what everything else
    eventually reads."""
    otoken: Optional[str] = None
    for item in items:
        kind = item.get("type")
        if kind not in ("Lakehouse", "Warehouse"):
            continue
        item_id = item.get("id")
        sub = "lakehouses" if kind == "Lakehouse" else "warehouses"
        meta_path = os.path.join(folder, sub, item_id + ".json")
        tables_path = os.path.join(folder, sub, item_id + ".tables.json")
        if not refresh and not _stale(tables_path, stale_after_days):
            continue
        try:
            meta = (api.lakehouse(ws_id, item_id, ftoken) if kind == "Lakehouse"
                    else api.warehouse(ws_id, item_id, ftoken))
            write_json(meta_path, meta)
        except Exception as exc:                      # noqa: BLE001
            _log("  [warn] " + kind + " " + str(item.get("displayName")) + ": " + str(exc)[:120])
            continue
        tables: List[Dict] = []
        if kind == "Lakehouse":
            try:
                tables = [{"schema": t.get("schema") or "dbo", "name": t.get("name"),
                           "format": t.get("format"), "location": t.get("location")}
                          for t in api.lakehouse_tables(ws_id, item_id, ftoken)]
            except Exception:                         # noqa: BLE001 - schema-enabled lakehouse
                tables = []
        if not tables:
            try:
                otoken = otoken or api.onelake_token()
                tables = api.onelake_tables(ws_id, item_id, otoken)
            except Exception as exc:                  # noqa: BLE001
                _log("  [warn] OneLake listing for " + str(item.get("displayName"))
                     + ": " + str(exc)[:120])
        write_json(tables_path, tables)
        _log("  " + kind.lower() + " " + str(item.get("displayName")) + ": "
             + str(len(tables)) + " tables")


def _harvest_scanner(raw: str, ws_ids: List[str], refresh: bool,
                     stale_after_days: float = 1.0) -> None:
    path = os.path.join(raw, "scanner.json")
    if not refresh and not _stale(path, stale_after_days):
        _log("scanner: cached")
        return
    try:
        result = api.scan(ws_ids, api.pbi_token())
    except Exception as exc:                          # noqa: BLE001 - needs admin + tenant settings
        _log("[warn] scanner unavailable (" + str(exc)[:200] + ")")
        _log("       endorsement, report->dataset binding and datasource lineage will be missing")
        return
    write_json(path, result)
    datasets = sum(len(w.get("datasets") or []) for w in result.get("workspaces") or [])
    with_dax = sum(1 for w in result.get("workspaces") or []
                   for d in w.get("datasets") or []
                   for t in d.get("tables") or []
                   for m in t.get("measures") or [] if m.get("expression"))
    measures = sum(len(t.get("measures") or []) for w in result.get("workspaces") or []
                   for d in w.get("datasets") or [] for t in d.get("tables") or [])
    _log("scanner: " + str(datasets) + " datasets, " + str(with_dax) + " of " + str(measures)
         + " measures with DAX")
    if datasets and not with_dax:
        _log("       [warn] no measure expressions - the tenant setting 'Enhance admin API "
             "responses with DAX and mashup expressions' is probably off")


def _harvest_activity(raw: str, days: int, refresh: bool) -> None:
    """One file per UTC day. Retention is 30 days; today and yesterday are always refetched
    because they are still filling up."""
    token = api.pbi_token()
    today = _utcnow().date()
    total = 0
    for back in range(min(days, 30)):
        day = (today - dt.timedelta(days=back)).isoformat()
        path = os.path.join(raw, "activity", day + ".json")
        if os.path.exists(path) and not refresh and back > 1:
            total += len(read_json(path, []) or [])
            continue
        try:
            rows = api.activity_events(day, token)
        except Exception as exc:                      # noqa: BLE001
            _log("[warn] activity " + day + ": " + str(exc)[:150])
            continue
        write_json(path, rows)
        total += len(rows)
    _log("activity: " + str(total) + " events over " + str(min(days, 30)) + " days")


# The DAX that actually ran. Identical query texts are collapsed in the engine rather than
# pulled row by row: a report page re-sends the same query on every open, so the counts are
# exact and the payload is a fraction of the raw event stream. EventText is truncated
# because a measure reference sits near the front of a query and a generated visual query
# can run to hundreds of kilobytes of filter context.
#
# ApplicationContext.Sources[0] says where the query came from and ApplicationName does not:
# a REST executeQueries call and a rendered report visual both report 'ReportServer', but
# only the visual carries a ReportId.
QUERY_LOG_KQL = """
SemanticModelLogs
| where Timestamp >= datetime({start}) and Timestamp < datetime({end})
| where OperationName == 'QueryEnd'
| where isnotempty(EventText)
| extend ctx = todynamic(ApplicationContext)
| extend ReportId = tostring(ctx.Sources[0].ReportId),
         Operation = tostring(ctx.Sources[0].Operation)
| extend Dax = substring(EventText, 0, {text_limit})
| summarize n = count(), users = dcount(ExecutingUser), last_ts = max(Timestamp),
            avg_ms = avg(DurationMs), cpu_ms = sum(CpuTimeMs)
        by ItemId, ItemName, WorkspaceId, ApplicationName, ReportId, Operation, Dax
| top {max_rows} by n desc
"""
QUERY_TEXT_LIMIT = 8000
QUERY_MAX_ROWS = 5000


def _harvest_query_log(ws_id: str, display: str, folder: str, ftoken: str,
                       days: int, refresh: bool) -> None:
    """One file per UTC day of QueryEnd events from the workspace's monitoring Eventhouse.

    Optional twice over: the workspace may not have monitoring enabled, and enabling it
    bills against the capacity. Neither case is an error - the ranking simply falls back to
    the report-reference proxy for that workspace.

    Retention is 30 days, matching the audit log; today and yesterday are always refetched
    because they are still filling up.
    """
    out = os.path.join(folder, "querylog")
    try:
        db = api.monitoring_database(ws_id, ftoken)
    except Exception as exc:                          # noqa: BLE001
        _log("  [warn] monitoring lookup for " + display + ": " + str(exc)[:150])
        return
    if not db or not db.get("cluster"):
        _log("  query log: no monitoring eventhouse in " + display
             + " (workspace settings -> Monitoring -> +Eventhouse)")
        return

    try:
        ktoken = api.kusto_token(db["cluster"])
    except Exception as exc:                          # noqa: BLE001
        _log("  [warn] no token for " + db["cluster"] + ": " + str(exc)[:150])
        return

    today = _utcnow().date()
    window = min(days, 30)
    total = rows_kept = 0
    for back in range(window):
        day = today - dt.timedelta(days=back)
        path = os.path.join(out, day.isoformat() + ".json")
        if os.path.exists(path) and not refresh and back > 1:
            total += sum(int(r.get("n") or 0) for r in read_json(path, []) or [])
            rows_kept += len(read_json(path, []) or [])
            continue
        kql = QUERY_LOG_KQL.format(
            start=day.isoformat() + "T00:00:00Z",
            end=(day + dt.timedelta(days=1)).isoformat() + "T00:00:00Z",
            text_limit=QUERY_TEXT_LIMIT, max_rows=QUERY_MAX_ROWS)
        try:
            rows = api.kql_query(db["cluster"], db["database"], kql, ktoken)
        except Exception as exc:                      # noqa: BLE001
            _log("  [warn] query log " + day.isoformat() + ": " + str(exc)[:150])
            continue
        write_json(path, rows)
        rows_kept += len(rows)
        total += sum(int(r.get("n") or 0) for r in rows)

    write_json(os.path.join(out, "manifest.json"),
               {"workspace_id": ws_id, "workspace": display, "database": db["database"],
                "cluster": db["cluster"], "days": window,
                "from": (today - dt.timedelta(days=window - 1)).isoformat(),
                "to": today.isoformat(), "harvested_at": _utcnow().isoformat()})
    _log("  query log: " + str(total) + " queries in " + str(rows_kept)
         + " distinct texts over " + str(window) + " days")
