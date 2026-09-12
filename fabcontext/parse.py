"""Step 2: raw/ -> build/nodes.jsonl, build/edges.jsonl, build/activity.jsonl, and - where
the workspace's query log was harvested - build/query_usage.jsonl and build/query_stats.jsonl.

Pure function of the raw folder: no network, so the interpretation can be rewritten and
re-run for free. Order matters - stores are indexed before models so a Direct Lake
partition can be bound, models before reports so a visual's field reference can be
resolved, and terms are mentioned last because the mention scan needs every term name.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .common import (GUID, Emitter, iso, node_id, read_json, term_id,
                    unresolved_id, walk_files)
from .parse_code import StoreIndex, parse_notebook, parse_pipeline
from .parse_model import ModelIndex, extract_dax_refs, parse_model
from .parse_report import parse_report

VIEW_ACTIVITIES = ("ViewReport", "ViewDashboard", "ViewTile", "ViewArtifact", "ReadArtifact",
                   "ViewDataflow", "ViewSemanticModel", "ViewDataset")
ITEM_ID_FIELDS = ("ArtifactId", "ReportId", "DatasetId", "DashboardId", "DataflowId", "ObjectId")


def _log(msg: str) -> None:
    print(msg, flush=True)


class Scanner:
    """Flat lookups over the scanner result, which nests everything under workspaces."""

    def __init__(self, result: Dict):
        self.datasets: Dict[str, Dict] = {}
        self.reports: Dict[str, Dict] = {}
        self.dataflows: Dict[str, Dict] = {}
        self.dashboards: Dict[str, Dict] = {}
        self.items: Dict[str, Dict] = {}
        self.datasources: Dict[str, Dict] = {
            d.get("datasourceId"): d for d in (result.get("datasourceInstances") or [])
            if d.get("datasourceId")}
        self.workspaces = result.get("workspaces") or []
        for ws in self.workspaces:
            for key, target in (("datasets", self.datasets), ("reports", self.reports),
                                ("dataflows", self.dataflows), ("dashboards", self.dashboards)):
                for row in ws.get(key) or []:
                    guid = row.get("id") or row.get("objectId")
                    if guid:
                        target[guid] = dict(row, workspaceId=ws.get("id"))
            for key, rows in ws.items():
                if key in ("datasets", "reports", "dataflows", "dashboards", "users"):
                    continue
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("id"):
                            self.items[row["id"]] = dict(row, workspaceId=ws.get("id"),
                                                         _kind=key)


def build(raw_dir: str, build_dir: str) -> Dict[str, int]:
    g = Emitter(build_dir)
    scanner = Scanner(read_json(os.path.join(raw_dir, "scanner.json"), {}) or {})

    ws_folders = sorted(
        os.path.join(raw_dir, d) for d in os.listdir(raw_dir)
        if os.path.isdir(os.path.join(raw_dir, d))
        and os.path.exists(os.path.join(raw_dir, d, "workspace.json")))
    if not ws_folders:
        raise SystemExit("no harvested workspaces under " + raw_dir + " - run harvest first")

    stores = StoreIndex()
    item_kind: Dict[str, str] = {}
    item_ws: Dict[str, str] = {}
    modified: Dict[str, str] = {}
    owners: Dict[str, str] = {}
    sql_endpoint_to_lakehouse: Dict[str, str] = {}
    contexts: List[Dict] = []

    # --- pass 1: inventory and physical stores -----------------------------------
    for folder in ws_folders:
        ws = read_json(os.path.join(folder, "workspace.json"), {}) or {}
        ws_name = ws.get("displayName") or os.path.basename(folder)
        ws_id = ws.get("id")
        items = read_json(os.path.join(folder, "items.json"), []) or []
        admin = read_json(os.path.join(folder, "admin_items.json"), []) or []
        for row in admin:
            if row.get("id"):
                modified[row["id"]] = iso(row.get("lastUpdatedDate"))
                creator = (row.get("creatorPrincipal") or {})
                owners[row["id"]] = (creator.get("displayName")
                                     or (creator.get("userDetails") or {}).get("userPrincipalName"))

        wid = node_id("workspace", ws_id)
        items_path = os.path.join(folder, "items.json")
        harvested_at = (dt.datetime.fromtimestamp(os.path.getmtime(items_path),
                                                  dt.timezone.utc)
                        .replace(tzinfo=None).isoformat()
                        if os.path.exists(items_path) else None)
        g.node(wid, "workspace", ws_name, workspace=ws_name, item_id=ws_id,
               description=ws.get("description"), capacity_id=ws.get("capacityId"),
               n_items=len(items), harvested_at=harvested_at)

        for item in items:
            guid, kind = item.get("id"), item.get("type")
            if not guid:
                continue
            item_kind[guid] = kind
            item_ws[guid] = ws_name
            g.edge(wid, _item_node(guid, kind), "contains")

        _emit_stores(folder, items, ws_name, stores, sql_endpoint_to_lakehouse, g, modified)
        contexts.append({"folder": folder, "ws_name": ws_name, "ws_id": ws_id, "items": items})

    # {store display name (lower) -> store item id}, for M that names a warehouse by name.
    store_names = dict(stores.store_by_name)

    # --- pass 2: semantic models --------------------------------------------------
    index_by_model: Dict[str, ModelIndex] = {}
    for ctx in contexts:
        for item, folder in _definitions(ctx, "SemanticModel"):
            bim = read_json(os.path.join(folder, "model.bim"), {}) or {}
            if not bim:
                continue
            index_by_model[item["id"]] = parse_model(
                bim, item, ctx["ws_name"], g,
                scanner_dataset=scanner.datasets.get(item["id"]),
                sql_endpoint_to_lakehouse=sql_endpoint_to_lakehouse,
                store_names=store_names, modified_at=modified.get(item["id"]))
    _log("  models parsed: " + str(len(index_by_model)))

    # --- pass 3: reports ----------------------------------------------------------
    n_reports = 0
    for ctx in contexts:
        for item, folder in _definitions(ctx, "Report"):
            parse_report(folder, item, ctx["ws_name"], g, index_by_model,
                         scanner_report=scanner.reports.get(item["id"]),
                         modified_at=modified.get(item["id"]))
            n_reports += 1
    _log("  reports parsed: " + str(n_reports))

    # --- pass 4: code -------------------------------------------------------------
    blobs: List[Tuple[str, str]] = []
    n_nb = n_pl = 0
    for ctx in contexts:
        for item, folder in _definitions(ctx, "Notebook"):
            nb = read_json(os.path.join(folder, "notebook-content.ipynb"), {}) or {}
            if not nb:
                continue
            parse_notebook(nb, item, ctx["ws_name"], g, stores, set(item_kind),
                           modified_at=modified.get(item["id"]), owner=owners.get(item["id"]))
            blobs.append((node_id("notebook", item["id"]), _notebook_text(nb)))
            n_nb += 1
        for item, folder in _definitions(ctx, "DataPipeline"):
            pipeline = read_json(os.path.join(folder, "pipeline-content.json"), {}) or {}
            if not pipeline:
                continue
            parse_pipeline(pipeline, item, ctx["ws_name"], g, stores, item_kind,
                           modified_at=modified.get(item["id"]), owner=owners.get(item["id"]))
            n_pl += 1
    _log("  notebooks parsed: " + str(n_nb) + ", pipelines parsed: " + str(n_pl))

    # --- pass 4b: stores outside the harvest get stub nodes, not dangling edges ---
    n_ext = _emit_external_stubs(g, stores)
    _log("  external stores stubbed: " + str(n_ext))

    # --- pass 4c: profiles gathered from the lakehouse copied onto model columns ---
    n_prof = _attach_profiles(g)
    _log("  model columns profiled: " + str(n_prof))

    # --- pass 5: everything the scanner knows and the definitions do not ----------
    _emit_scanner(scanner, item_kind, item_ws, g)

    # --- pass 6: leftover inventory, so no item is missing from the graph ---------
    for ctx in contexts:
        for item in ctx["items"]:
            _ensure_node(item, ctx["ws_name"], modified, owners, scanner, g)

    # --- pass 7: activity ---------------------------------------------------------
    views = _emit_activity(raw_dir, set(item_kind), item_kind, g)
    queries = _emit_query_log(contexts, index_by_model, g)
    _log("  activity events kept: " + str(views))
    if queries:
        _log("  queries attributed to measures: " + str(queries))

    # --- pass 8: mentions ---------------------------------------------------------
    n_mentions = _emit_mentions(blobs, g)
    _log("  term mentions in code: " + str(n_mentions))

    counts = g.flush()
    _log("  wrote " + str(counts["nodes"]) + " nodes, " + str(counts["edges"]) + " edges, "
         + str(counts["activity"]) + " activity rows"
         + (", " + str(counts["query_usage"]) + " query-usage rows"
            if counts.get("query_usage") else ""))
    return counts


# ---------------------------------------------------------------- helpers

_KIND_NODE = {"SemanticModel": "semantic_model", "Report": "report", "Notebook": "notebook",
              "DataPipeline": "pipeline", "Lakehouse": "lakehouse", "Warehouse": "warehouse",
              "Dataflow": "dataflow", "Dashboard": "dashboard",
              "SQLEndpoint": "sql_endpoint", "SQLAnalyticsEndpoint": "sql_endpoint"}


def _item_node(guid: str, kind: Optional[str]) -> str:
    return node_id(_KIND_NODE.get(kind or "", "other_item"), guid)


def _definitions(ctx: Dict, item_type: str):
    """(item.json, folder) for every harvested definition of one type in a workspace."""
    base = os.path.join(ctx["folder"], "definitions", item_type)
    if not os.path.isdir(base):
        return
    for guid in sorted(os.listdir(base)):
        folder = os.path.join(base, guid)
        item = read_json(os.path.join(folder, "item.json"), None)
        if item:
            yield item, folder


def _emit_stores(folder: str, items: List[Dict], ws_name: str, stores: StoreIndex,
                 sql_map: Dict[str, str], g: Emitter, modified: Dict[str, str]) -> None:
    for item in items:
        kind = item.get("type")
        if kind not in ("Lakehouse", "Warehouse"):
            continue
        guid = item["id"]
        sub = "lakehouses" if kind == "Lakehouse" else "warehouses"
        meta = read_json(os.path.join(folder, sub, guid + ".json"), {}) or {}
        tables = read_json(os.path.join(folder, sub, guid + ".tables.json"), []) or []
        profiles = read_json(os.path.join(folder, "profiles", guid + ".json"), {}) or {}
        node_kind = "lakehouse" if kind == "Lakehouse" else "warehouse"
        sid = node_id(node_kind, guid)
        name = item.get("displayName") or guid
        endpoint = ((meta.get("properties") or {}).get("sqlEndpointProperties") or {})
        if endpoint.get("id"):
            sql_map[str(endpoint["id"]).lower()] = guid
        sql_map[str(name).lower()] = guid            # a warehouse is named in M by name
        g.node(sid, node_kind, name, workspace=ws_name, item_id=guid,
               description=item.get("description"), modified_at=modified.get(guid),
               n_tables=len(tables), sql_endpoint_id=endpoint.get("id"),
               sql_endpoint_server=(endpoint.get("connectionString")
                                    or (meta.get("properties") or {}).get("connectionString")),
               onelake_path=(meta.get("properties") or {}).get("oneLakeTablesPath"))
        for table in tables:
            schema = table.get("schema") or "dbo"
            tname = table.get("name")
            if not tname:
                continue
            tid = stores.add(guid, schema, tname, store_name=name)
            prof = profiles.get(schema + "." + tname) or {}
            g.node(tid, "lakehouse_table", tname, workspace=ws_name, item_id=guid,
                   parent_id=sid, schema=schema, store=name, store_kind=node_kind,
                   format=table.get("format"),
                   columns=prof.get("columns"), n_rows=prof.get("n_rows"),
                   n_files=prof.get("n_files"), version=prof.get("version"),
                   partition_columns=prof.get("partition_columns"),
                   stats=prof.get("stats"), values=prof.get("values"),
                   n_distinct=prof.get("n_distinct"), profiled_at=prof.get("profiled_at"))
            g.edge(sid, tid, "contains")
            for col in prof.get("columns") or []:
                cname = col.get("name")
                if not cname:
                    continue
                col["type"] = _plain_type(col.get("type"))
                cid = node_id("column", guid, schema, tname, cname)
                g.node(cid, "column", cname, workspace=ws_name, item_id=guid, parent_id=tid,
                       data_type=col["type"], table=tname, schema=schema, store=name,
                       profile=_column_profile(prof, cname))
                g.edge(tid, cid, "contains")


_TYPE_WRAPPER = re.compile(r'^\w+Type\("?([^")]+)"?\)$')


def _plain_type(value) -> Optional[str]:
    """'string' from either an Arrow type name or delta-rs's PrimitiveType("string")."""
    if value is None:
        return None
    match = _TYPE_WRAPPER.match(str(value))
    return match.group(1) if match else str(value)


def _column_profile(prof: Dict, cname: str) -> Optional[Dict]:
    """{min, max, null_frac, n_distinct, values} for one column of a profiled table, or
    None when the profile says nothing about it."""
    out: Dict = {}
    stats = (prof.get("stats") or {}).get(cname) or {}
    for key in ("min", "max", "null_frac"):
        if stats.get(key) is not None:
            out[key] = stats[key]
    ndv = (prof.get("n_distinct") or {}).get(cname)
    if ndv is not None:
        out["n_distinct"] = ndv
    values = (prof.get("values") or {}).get(cname)
    if values:
        out["values"] = values
    return out or None


def _stub_name(store: str) -> str:
    return store[:8] + " (external)"


def _stub_store(g: Emitter, store: str, ws_id: Optional[str], via: Optional[str]) -> str:
    sid = node_id("lakehouse", store)
    g.node(sid, "lakehouse", _stub_name(store), item_id=store, external=True,
           workspace_id=ws_id, via=via,
           onelake_path=("https://onelake.dfs.fabric.microsoft.com/" + ws_id + "/" + store
                         + "/Tables") if ws_id else None)
    return sid


def _emit_external_stubs(g: Emitter, stores: StoreIndex) -> int:
    """A model or notebook bound to a store outside the harvested workspaces gets a stub
    node, so the edge resolves and a reader sees 'external' instead of nothing. The stub
    keeps the workspace id when the binding named one; through a SQL endpoint it only has
    the endpoint id, and says so in `via`."""
    # Every harvested store, including one with no tables (stores.store_ids only knows
    # stores that had tables to index).
    known = set(stores.store_ids) | {row.get("item_id") for row in g.nodes.rows()
                                     if row.get("kind") in ("lakehouse", "warehouse")
                                     and row.get("item_id")}
    made = 0
    for edge in list(g.edges.rows()):
        dst = edge.get("dst") or ""
        attrs = edge.get("attrs") or {}
        if dst.startswith("lakehouse_table:"):
            parts = dst.split(":", 1)[1].split("/", 2)
            if len(parts) != 3 or parts[0] in known:
                continue
            store, schema, table = parts
            sid = _stub_store(g, store, attrs.get("source_workspace_id"), attrs.get("source_via"))
            g.node(dst, "lakehouse_table", table, item_id=store, parent_id=sid, schema=schema,
                   store=_stub_name(store), store_kind="lakehouse", external=True)
            g.edge(sid, dst, "contains")
            made += 1
        elif dst.startswith("lakehouse:"):
            store = dst.split(":", 1)[1]
            if store in known or not GUID.fullmatch(store):
                continue
            _stub_store(g, store, None, "default lakehouse")
            made += 1
    return made


def _attach_profiles(g: Emitter) -> int:
    """Copy a lakehouse column's profile onto the model column that reads it, through the
    sources_from edge and the column's sourceColumn. Direct Lake columns ARE the lakehouse
    columns, so the copy is exact."""
    table_prof: Dict[str, Dict] = {}
    columns_of: Dict[str, List[Dict]] = {}
    by_id: Dict[str, Dict] = {}
    for row in g.nodes.rows():
        by_id[row["id"]] = row
        kind = row.get("kind")
        attrs = row.get("attrs") or {}
        if kind == "lakehouse_table" and (attrs.get("stats") or attrs.get("values")
                                          or attrs.get("n_distinct")):
            table_prof[row["id"]] = attrs
        elif kind == "column" and str(row.get("parent_id") or "").startswith("model_table:"):
            columns_of.setdefault(row["parent_id"], []).append(row)
    if not table_prof:
        return 0
    done = 0
    for edge in g.edges.rows():
        if edge.get("rel") != "sources_from":
            continue
        prof = table_prof.get(edge.get("dst"))
        if not prof:
            continue
        for col in columns_of.get(edge.get("src"), []):
            attrs = col.setdefault("attrs", {})
            if attrs.get("profile"):
                continue
            profile = _column_profile(prof, attrs.get("source_column") or col.get("name"))
            if profile:
                attrs["profile"] = profile
                done += 1
        owner = by_id.get(edge.get("src"))
        if owner is not None and prof.get("n_rows") is not None:
            owner.setdefault("attrs", {}).setdefault("n_rows", prof["n_rows"])
    return done


def _ensure_node(item: Dict, ws_name: str, modified: Dict[str, str], owners: Dict[str, str],
                 scanner: "Scanner", g: Emitter) -> None:
    """Every item gets a node even when it has no definition and no scanner entry, so the
    workspace contains edges never dangle."""
    guid, kind = item.get("id"), item.get("type")
    if not guid:
        return
    nid = _item_node(guid, kind)
    meta = (scanner.items.get(guid) or scanner.datasets.get(guid) or scanner.reports.get(guid)
            or scanner.dataflows.get(guid) or {})
    g.node(nid, _KIND_NODE.get(kind or "", "other_item"), item.get("displayName") or guid,
           workspace=ws_name, item_id=guid, description=item.get("description"),
           endorsement=(meta.get("endorsementDetails") or {}).get("endorsement"),
           owner=owners.get(guid) or meta.get("configuredBy") or meta.get("modifiedBy"),
           modified_at=modified.get(guid) or iso(meta.get("modifiedDateTime")),
           item_type=kind)


def _emit_scanner(scanner: Scanner, item_kind: Dict[str, str], item_ws: Dict[str, str],
                  g: Emitter) -> None:
    """Endorsement, cross-item lineage and datasources - none of which appear in an item's
    own definition."""
    for guid, ds in scanner.datasets.items():
        for up in ds.get("upstreamDataflows") or []:
            if up.get("targetDataflowId"):
                g.edge(node_id("semantic_model", guid),
                       node_id("dataflow", up["targetDataflowId"]), "uses", via="scanner")
        for up in ds.get("upstreamDatasets") or []:
            if up.get("targetDatasetId"):
                g.edge(node_id("semantic_model", guid),
                       node_id("semantic_model", up["targetDatasetId"]), "uses", via="scanner")
        for use in ds.get("datasourceUsages") or []:
            _datasource(use, node_id("semantic_model", guid), scanner, g)

    for guid, df in scanner.dataflows.items():
        nid = node_id("dataflow", guid)
        g.node(nid, "dataflow", df.get("name") or guid, workspace=item_ws.get(guid),
               item_id=guid, description=df.get("description"),
               endorsement=(df.get("endorsementDetails") or {}).get("endorsement"),
               owner=df.get("configuredBy") or df.get("modifiedBy"),
               modified_at=iso(df.get("modifiedDateTime")), generation=df.get("generation"))
        for up in df.get("upstreamDataflows") or []:
            if up.get("targetDataflowId"):
                g.edge(nid, node_id("dataflow", up["targetDataflowId"]), "uses", via="scanner")
        for use in df.get("datasourceUsages") or []:
            _datasource(use, nid, scanner, g)

    for guid, dash in scanner.dashboards.items():
        nid = node_id("dashboard", guid)
        tiles = dash.get("tiles") or []
        g.node(nid, "dashboard", dash.get("displayName") or guid,
               workspace=item_ws.get(guid), item_id=guid, n_tiles=len(tiles),
               endorsement=(dash.get("endorsementDetails") or {}).get("endorsement"))
        for tile in tiles:
            if tile.get("reportId"):
                g.edge(nid, node_id("report", tile["reportId"]), "uses", via="tile")
            if tile.get("datasetId"):
                g.edge(nid, node_id("semantic_model", tile["datasetId"]), "uses", via="tile")

    for guid, item in scanner.items.items():
        for rel in item.get("relations") or []:
            target = rel.get("dependentOnArtifactId")
            if target and target in item_kind:
                g.edge(_item_node(guid, item_kind.get(guid)),
                       _item_node(target, item_kind.get(target)), "depends_on",
                       relation=rel.get("relationType"))


def _datasource(use: Dict, src: str, scanner: Scanner, g: Emitter) -> None:
    dsid = use.get("datasourceInstanceId")
    if not dsid:
        return
    meta = scanner.datasources.get(dsid) or {}
    details = meta.get("connectionDetails") or {}
    label = (details.get("server") or details.get("url") or details.get("path")
             or meta.get("datasourceType") or dsid)
    database = details.get("database")
    nid = node_id("datasource", dsid)
    g.node(nid, "datasource", str(label) + ("/" + str(database) if database else ""),
           datasource_type=meta.get("datasourceType"), server=details.get("server"),
           database=database, url=details.get("url"), gateway_id=meta.get("gatewayId"))
    g.edge(src, nid, "sources_from", via="scanner")


# Where the query came from. ApplicationName cannot answer it - a REST executeQueries call
# and a rendered report visual both report 'ReportServer' - but only a visual carries a
# ReportId in ApplicationContext.Sources. The name is the fallback for a row with no
# context at all. The split is a reporting dimension, not a ranking weight: a query is a
# query, and both are counted the same in `queries`.
_REPORT_CLIENTS = ("power bi", "powerbi", "dashboard", "mashup")


def _is_report_query(row: Dict) -> bool:
    if row.get("ReportId"):
        return True
    if row.get("Operation"):
        return False                      # a named non-report operation, e.g. ExecuteQueries
    low = str(row.get("ApplicationName") or "").lower()
    return any(hint in low for hint in _REPORT_CLIENTS)


def _emit_query_log(contexts: List[Dict], index_by_model: Dict[str, ModelIndex],
                    g: Emitter) -> int:
    """The DAX that actually ran, attributed to the measures it called.

    Where the report-reference proxy asks "is this measure written into a report someone
    opens", this asks the direct question: was it evaluated, how often, by how many people.
    Only workspaces whose monitoring Eventhouse was harvested have any; the rest keep the
    proxy, so partial coverage degrades to today's behaviour rather than to zero.

    The query text never leaves raw/. What lands here is counts.
    """
    per_def: Dict[str, Dict[str, Any]] = {}
    per_model: Dict[str, Dict[str, Any]] = {}
    total = 0

    for ctx in contexts:
        folder = os.path.join(ctx["folder"], "querylog")
        manifest = read_json(os.path.join(folder, "manifest.json"), None)
        if not manifest:
            continue
        # Every model in a monitored workspace gets a row, so "nobody queried it" is
        # distinguishable from "nobody was watching".
        for item in ctx["items"]:
            if item.get("type") == "SemanticModel" and item.get("id"):
                per_model.setdefault(item["id"], {
                    "workspace_id": ctx["ws_id"], "workspace": ctx["ws_name"],
                    "name": item.get("displayName"), "queries": 0, "texts": 0,
                    "report_queries": 0, "adhoc_queries": 0, "measureless_queries": 0,
                    "users": 0, "last_queried": None, "window_days": manifest.get("days"),
                    "from_day": manifest.get("from"), "to_day": manifest.get("to")})

        for path in sorted(walk_files(folder)):
            if os.path.basename(path) == "manifest.json":
                continue
            for row in read_json(path, []) or []:
                model_id = row.get("ItemId")
                n = int(row.get("n") or 0)
                if not model_id or n <= 0:
                    continue
                stat = per_model.setdefault(model_id, {
                    "workspace_id": ctx["ws_id"], "workspace": ctx["ws_name"],
                    "name": row.get("ItemName"), "queries": 0, "texts": 0,
                    "report_queries": 0, "adhoc_queries": 0, "measureless_queries": 0,
                    "users": 0, "last_queried": None, "window_days": manifest.get("days"),
                    "from_day": manifest.get("from"), "to_day": manifest.get("to")})
                users = int(row.get("users") or 0)
                last = iso(row.get("last_ts"))
                stat["queries"] += n
                stat["texts"] += 1
                stat["users"] = max(stat["users"], users)
                stat["last_queried"] = max(stat["last_queried"] or "", last or "") or None
                if _is_report_query(row):
                    stat["report_queries"] += n
                else:
                    stat["adhoc_queries"] += n
                total += n

                index = index_by_model.get(model_id)
                refs = extract_dax_refs(row.get("Dax") or "", index) if index else {}
                measures = [nid for nid in refs if nid.startswith("measure:")]
                if not measures:
                    # No measure called: either the query names only columns - the
                    # re-derivation this layer exists to make visible - or the model was
                    # never parsed.
                    stat["measureless_queries"] += n
                    continue
                for nid in measures:
                    acc = per_def.setdefault(nid, {
                        "item_id": model_id, "workspace_id": ctx["ws_id"],
                        "queries": 0, "report_queries": 0, "adhoc_queries": 0,
                        "users": 0, "last_queried": None})
                    acc["queries"] += n
                    acc["users"] = max(acc["users"], users)
                    acc["last_queried"] = max(acc["last_queried"] or "", last or "") or None
                    if _is_report_query(row):
                        acc["report_queries"] += n
                    else:
                        acc["adhoc_queries"] += n

    for def_id, acc in per_def.items():
        g.queried(def_id, **acc)
    for item_id, stat in per_model.items():
        g.query_stat(item_id, **stat)
    return total


def _emit_activity(raw_dir: str, known: Set[str], item_kind: Dict[str, str],
                   g: Emitter) -> int:
    folder = os.path.join(raw_dir, "activity")
    if not os.path.isdir(folder):
        return 0
    views: Dict[Tuple[str, str], int] = {}
    kept = 0
    for path in sorted(walk_files(folder)):
        for row in read_json(path, []) or []:
            guid = next((row.get(f) for f in ITEM_ID_FIELDS if row.get(f) in known), None)
            if not guid:
                continue
            kept += 1
            g.event(row.get("Id") or (str(row.get("CreationTime")) + str(guid)),
                    ts=iso(row.get("CreationTime")), activity=row.get("Activity"),
                    user_id=row.get("UserId"), workspace_id=row.get("WorkspaceId"),
                    item_id=guid, item_kind=row.get("ArtifactKind") or item_kind.get(guid),
                    attrs=json.dumps({"name": row.get("ItemName") or row.get("ArtifactName"),
                                      "consumption": row.get("ConsumptionMethod")},
                                     ensure_ascii=False))
            if row.get("Activity") in VIEW_ACTIVITIES and row.get("UserId"):
                key = (guid, str(row["UserId"]).lower())
                views[key] = views.get(key, 0) + 1
    for (guid, user), count in views.items():
        uid = node_id("user", user)
        g.node(uid, "user", user)
        g.edge(_item_node(guid, item_kind.get(guid)), uid, "viewed_by", weight=count)
    return kept


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9 _%#-]{3,60}")


def _notebook_text(nb: Dict) -> str:
    out = []
    for cell in nb.get("cells") or []:
        src = cell.get("source")
        out.append("".join(src) if isinstance(src, list) else str(src or ""))
    return "\n".join(out)


def _emit_mentions(blobs: List[Tuple[str, str]], g: Emitter) -> int:
    """A term named in notebook code, without any table binding, is still a signal that the
    code is about that term."""
    aliases: Dict[str, str] = {}
    for row in g.nodes.rows():
        if row.get("kind") not in ("measure", "report_measure"):
            continue
        name = row.get("name") or ""
        if len(name) >= 5:
            aliases.setdefault(name.lower(), term_id(name))
    if not aliases:
        return 0
    pattern = re.compile(
        r"(?<![\w])(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)[:400])
        + r")(?![\w])", re.I)
    count = 0
    for nid, text in blobs:
        seen: Dict[str, int] = {}
        for match in pattern.finditer(text):
            tid = aliases.get(match.group(1).lower())
            if tid:
                seen[tid] = seen.get(tid, 0) + 1
        for tid, hits in seen.items():
            g.edge(nid, node_id("term", tid), "mentions", weight=hits)
            count += 1
    return count


if __name__ == "__main__":
    build()
