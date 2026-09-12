"""End-to-end test on a synthetic tenant: parse -> graph -> wiki, no Fabric needed.

Builds a raw/ folder that exercises every parser - a Direct Lake model, a second model whose
DAX for the same term disagrees, a PBIR report, a legacy report, a notebook, a pipeline, a
scanner result and 28 days of activity - then asserts the ranking and the wiki come out
right. Run it after changing any parser:  python selftest.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile

WS = "11111111-1111-1111-1111-111111111111"
LH = "22222222-2222-2222-2222-222222222222"
MODEL_A = "33333333-3333-3333-3333-333333333333"
MODEL_B = "44444444-4444-4444-4444-444444444444"
REPORT_A = "55555555-5555-5555-5555-555555555555"
REPORT_B = "66666666-6666-6666-6666-666666666666"
NOTEBOOK = "77777777-7777-7777-7777-777777777777"
PIPELINE = "88888888-8888-8888-8888-888888888888"
WS2 = "99999999-1111-1111-1111-111111111111"          # a workspace nobody harvested
LH2 = "99999999-2222-2222-2222-222222222222"          # its lakehouse, bound by model B


def _w(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)


def _model_a():
    return {"compatibilityLevel": 1604, "model": {
        "expressions": [{"name": "DirectLake", "kind": "m", "expression": [
            "let",
            '    Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/'
            + WS + "/" + LH + '", [HierarchicalNavigation=true])',
            "in", "    Source"]}],
        "tables": [
            {"name": "Sales", "description": "Order lines.",
             "partitions": [{"name": "Sales", "mode": "directLake", "source": {
                 "type": "entity", "entityName": "fact_sales", "schemaName": "dbo",
                 "expressionSource": "DirectLake"}}],
             "columns": [{"name": "Amount", "dataType": "decimal"},
                         {"name": "OrderDate", "dataType": "dateTime"},
                         {"name": "CustomerKey", "dataType": "int64"},
                         {"name": "Region", "dataType": "string", "sourceColumn": "Region"}],
             "measures": [
                 {"name": "Total Revenue", "description": "Net of returns.",
                  "expression": "SUM ( Sales[Amount] )"},
                 {"name": "Price_AVG", "expression": "AVERAGE ( Sales[Amount] )"},
                 {"name": "Revenue YTD",
                  "expression": "TOTALYTD ( [Total Revenue], 'Date'[Date] ) -- [Ignored]"},
             ]},
            {"name": "Date",
             "partitions": [{"name": "Date", "mode": "directLake", "source": {
                 "type": "entity", "entityName": "dim_date", "schemaName": "dbo",
                 "expressionSource": "DirectLake"}}],
             "columns": [{"name": "Date", "dataType": "dateTime"}], "measures": []},
        ],
        "relationships": [{"name": "r1", "fromTable": "Sales", "fromColumn": "OrderDate",
                           "toTable": "Date", "toColumn": "Date"}]}}


def _model_b():
    return {"compatibilityLevel": 1604, "model": {
        "expressions": [{"name": "ExtLake", "kind": "m", "expression": [
            "let",
            '    Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/'
            + WS2 + "/" + LH2 + '", [HierarchicalNavigation=true])',
            "in", "    Source"]}],
        "tables": [
        {"name": "External",
         "partitions": [{"name": "External", "mode": "directLake", "source": {
             "type": "entity", "entityName": "ext_table", "schemaName": "dbo",
             "expressionSource": "ExtLake"}}],
         "columns": [{"name": "X", "dataType": "string"}], "measures": []},
        {"name": "Ledger",
         "partitions": [{"name": "Ledger", "mode": "import", "source": {
             "type": "m", "expression": [
                 "let",
                 '    Source = Sql.Database("abc.datawarehouse.fabric.microsoft.com", "sales_lh"),',
                 '    dbo_fact_sales = Source{[Schema="dbo",Item="fact_sales"]}[Data]',
                 "in", "    dbo_fact_sales"]}}],
         "columns": [{"name": "Amount", "dataType": "decimal"},
                     {"name": "IsInternal", "dataType": "boolean"}],
         "measures": [{"name": "Revenue",
                       "expression": "SUMX ( FILTER ( Ledger, Ledger[IsInternal] = FALSE ), "
                                     "Ledger[Amount] )"},
                      {"name": "Average Price",
                       "expression": "AVERAGEX ( Ledger, Ledger[Amount] )"}]}]}}


def _pbir_report(folder):
    _w(os.path.join(folder, "definition.pbir"),
       {"datasetReference": {"byConnection": {"pbiModelDatabaseName": MODEL_A}}})
    pages = os.path.join(folder, "definition", "pages")
    _w(os.path.join(pages, "pages.json"), {"pageOrder": ["p1"], "activePageName": "p1"})
    _w(os.path.join(pages, "p1", "page.json"), {"name": "p1", "displayName": "Overview"})
    for vis, prop in (("v1", "Total Revenue"), ("v2", "Revenue YTD")):
        _w(os.path.join(pages, "p1", "visuals", vis, "visual.json"), {
            "name": vis, "visual": {"visualType": "card", "query": {"queryState": {"Values": {
                "projections": [{"field": {"Measure": {
                    "Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": prop}}}]}}}}})
    _w(os.path.join(folder, "definition", "reportExtensions.json"), {"entities": [
        {"name": "Sales", "measures": [
            {"name": "Revenue", "expression": "SUM ( Sales[Amount] ) * 1.1"}]}]})


def _legacy_report(folder):
    _w(os.path.join(folder, "report.json"), {"sections": [{
        "name": "s1", "displayName": "Ledger", "ordinal": 0, "visualContainers": [{
            "config": json.dumps({"name": "vx", "singleVisual": {
                "visualType": "barChart",
                "projections": {"Values": [{"queryRef": "Ledger.Revenue"}]},
                "prototypeQuery": {
                    "From": [{"Name": "l", "Entity": "Ledger", "Type": 0}],
                    "Select": [{"Measure": {"Expression": {"SourceRef": {"Source": "l"}},
                                            "Property": "Revenue"}, "Name": "Ledger.Revenue"}]}}})
        }]}]})


def _notebook():
    return {"cells": [
        {"cell_type": "code", "source": [
            "df = spark.table('dbo.dim_customer')\n",
            "df.write.mode('overwrite').saveAsTable('dbo.fact_sales')\n",
            "# recompute Total Revenue for the mart\n"]},
        {"cell_type": "code", "source": "spark.sql('SELECT * FROM dbo.dim_date')"},
        {"cell_type": "code", "source": "from deltalake import write_deltalake\n"
                                        "import psutil, shutil\n"},
    ], "metadata": {"dependencies": {"lakehouse": {
        "default_lakehouse": LH, "default_lakehouse_name": "sales_lh",
        "default_lakehouse_workspace_id": WS}}}}


def _pipeline():
    return {"properties": {"activities": [
        {"name": "load", "type": "TridentNotebook",
         "typeProperties": {"notebookId": NOTEBOOK, "workspaceId": WS}},
        {"name": "refresh", "type": "PBISemanticModelRefresh",
         "typeProperties": {"datasetId": MODEL_A}},
    ]}}


def _scanner():
    return {"workspaces": [{
        "id": WS, "name": "Sales Demo", "type": "Workspace",
        "datasets": [
            {"id": MODEL_A, "name": "Sales Model", "configuredBy": "amal@example.com",
             "endorsementDetails": {"endorsement": "Certified", "certifiedBy": "governance"},
             "targetStorageMode": "DirectLake"},
            {"id": MODEL_B, "name": "Finance Model", "configuredBy": "rui@example.com"}],
        "reports": [
            {"id": REPORT_A, "name": "Sales Overview", "datasetId": MODEL_A,
             "modifiedBy": "amal@example.com", "modifiedDateTime": "2026-08-01T00:00:00"},
            {"id": REPORT_B, "name": "Ledger Detail", "datasetId": MODEL_B,
             "modifiedBy": "rui@example.com", "modifiedDateTime": "2026-02-01T00:00:00"}],
        "Lakehouse": [{"id": LH, "name": "sales_lh",
                       "relations": [{"dependentOnArtifactId": NOTEBOOK,
                                      "relationType": "Fed"}]}],
    }], "datasourceInstances": []}


def make_raw(raw: str) -> None:
    ws_dir = os.path.join(raw, "Sales-Demo--11111111")
    items = [
        {"id": MODEL_A, "type": "SemanticModel", "displayName": "Sales Model"},
        {"id": MODEL_B, "type": "SemanticModel", "displayName": "Finance Model"},
        {"id": REPORT_A, "type": "Report", "displayName": "Sales Overview"},
        {"id": REPORT_B, "type": "Report", "displayName": "Ledger Detail"},
        {"id": NOTEBOOK, "type": "Notebook", "displayName": "load_sales"},
        {"id": PIPELINE, "type": "DataPipeline", "displayName": "nightly"},
        {"id": LH, "type": "Lakehouse", "displayName": "sales_lh"},
    ]
    _w(os.path.join(ws_dir, "workspace.json"),
       {"id": WS, "displayName": "Sales Demo", "capacityId": "cap1"})
    _w(os.path.join(ws_dir, "items.json"), items)
    today = dt.date.today()
    _w(os.path.join(ws_dir, "admin_items.json"), [
        {"id": it["id"], "lastUpdatedDate": (today - dt.timedelta(days=10)).isoformat()
         + "T00:00:00", "creatorPrincipal": {"displayName": "amal@example.com"}}
        for it in items])

    defs = os.path.join(ws_dir, "definitions")
    for guid, name, bim in ((MODEL_A, "Sales Model", _model_a()),
                            (MODEL_B, "Finance Model", _model_b())):
        folder = os.path.join(defs, "SemanticModel", guid)
        _w(os.path.join(folder, "item.json"),
           {"id": guid, "type": "SemanticModel", "displayName": name, "format": "TMSL"})
        _w(os.path.join(folder, "model.bim"), bim)

    folder = os.path.join(defs, "Report", REPORT_A)
    _w(os.path.join(folder, "item.json"),
       {"id": REPORT_A, "type": "Report", "displayName": "Sales Overview", "format": "PBIR"})
    _pbir_report(folder)

    folder = os.path.join(defs, "Report", REPORT_B)
    _w(os.path.join(folder, "item.json"), {"id": REPORT_B, "type": "Report",
                                           "displayName": "Ledger Detail",
                                           "format": "PBIR-Legacy"})
    _legacy_report(folder)

    folder = os.path.join(defs, "Notebook", NOTEBOOK)
    _w(os.path.join(folder, "item.json"),
       {"id": NOTEBOOK, "type": "Notebook", "displayName": "load_sales", "format": "ipynb"})
    _w(os.path.join(folder, "notebook-content.ipynb"), _notebook())

    folder = os.path.join(defs, "DataPipeline", PIPELINE)
    _w(os.path.join(folder, "item.json"),
       {"id": PIPELINE, "type": "DataPipeline", "displayName": "nightly"})
    _w(os.path.join(folder, "pipeline-content.json"), _pipeline())

    _w(os.path.join(ws_dir, "lakehouses", LH + ".json"),
       {"id": LH, "properties": {"sqlEndpointProperties": {"id": "99999999-9999-9999-9999-999999999999"},
                                 "oneLakeTablesPath": "abfss://x/Tables"}})
    # scratch_tmp is read by nothing: the long tail every real tenant is mostly made of,
    # here so the tiering in graph._tier has something to demote.
    _w(os.path.join(ws_dir, "lakehouses", LH + ".tables.json"),
       [{"schema": "dbo", "name": n, "format": "delta"}
        for n in ("fact_sales", "dim_customer", "dim_date", "scratch_tmp")])

    _w(os.path.join(raw, "scanner.json"), _scanner())

    _w(os.path.join(ws_dir, "profiles", LH + ".json"), {"dbo.fact_sales": {
        "columns": [{"name": "Amount", "type": "decimal(18,2)"},
                    {"name": "OrderDate", "type": "timestamp"},
                    {"name": "Region", "type": "string"}],
        "n_rows": 1200, "n_files": 2, "version": 5, "partition_columns": [],
        "stats": {"OrderDate": {"min": "2026-01-01", "max": "2026-08-31", "null_frac": 0.0}},
        "n_distinct": {"Region": 2}, "values": {"Region": ["NSW", "VIC"]},
        "profiled_at": "2026-09-10T00:00:00"}})

    for back in range(3):
        day = (today - dt.timedelta(days=back)).isoformat()
        events = []
        for i in range(20 if back == 0 else 8):
            events.append({"Id": day + "-a" + str(i), "CreationTime": day + "T09:00:00",
                           "Activity": "ViewReport", "UserId": "user" + str(i % 4) + "@example.com",
                           "WorkspaceId": WS, "ReportId": REPORT_A, "ArtifactId": REPORT_A,
                           "ItemName": "Sales Overview"})
        if back == 0:
            for i, act in enumerate(("RunArtifact", "ReadArtifact", "ItemDefinitionExported")):
                events.append({"Id": day + "-m" + str(i), "CreationTime": day + "T11:00:00",
                               "Activity": act, "UserId": "user1@example.com",
                               "WorkspaceId": WS, "ArtifactId": MODEL_A, "DatasetId": MODEL_A,
                               "ArtifactKind": "Dataset"})
        events.append({"Id": day + "-b", "CreationTime": day + "T10:00:00",
                       "Activity": "ViewReport", "UserId": "user9@example.com",
                       "WorkspaceId": WS, "ReportId": REPORT_B, "ArtifactId": REPORT_B,
                       "ItemName": "Ledger Detail"})
        _w(os.path.join(raw, "activity", day + ".json"), events)
    _make_query_log(raw)


def _make_query_log(raw: str) -> None:
    """A monitoring Eventhouse's QueryEnd rows for model A only, so the build exercises
    both sides of partial coverage: a monitored model, and one nobody watched.

    'Price_AVG' is queried far more than 'Total Revenue' while being referenced by no
    report, which is exactly the case the report-reference proxy cannot see."""
    ws_dir = os.path.join(raw, "Sales-Demo--11111111", "querylog")
    today = dt.date.today()
    _w(os.path.join(ws_dir, "manifest.json"),
       {"workspace_id": WS, "workspace": "Sales Demo", "database": "Monitoring",
        "cluster": "https://example.kusto.fabric.microsoft.com", "days": 3,
        "from": (today - dt.timedelta(days=2)).isoformat(), "to": today.isoformat()})
    for back in range(3):
        day = (today - dt.timedelta(days=back)).isoformat()
        _w(os.path.join(ws_dir, day + ".json"), [
            {"ItemId": MODEL_A, "ItemName": "Sales Model", "WorkspaceId": WS,
             "ApplicationName": "DAX Studio", "Dax": "EVALUATE ROW(\"v\", [Price_AVG])",
             "n": 40, "users": 3, "last_ts": day + "T09:15:00", "avg_ms": 120.0,
             "cpu_ms": 900},
            {"ItemId": MODEL_A, "ItemName": "Sales Model", "WorkspaceId": WS,
             "ApplicationName": "Power BI Client",
             "Dax": "EVALUATE SUMMARIZECOLUMNS(Sales[Region], \"r\", [Total Revenue])",
             "n": 5, "users": 2, "last_ts": day + "T10:00:00", "avg_ms": 80.0,
             "cpu_ms": 200},
            {"ItemId": MODEL_A, "ItemName": "Sales Model", "WorkspaceId": WS,
             "ApplicationName": "Excel",
             "Dax": "EVALUATE ROW(\"v\", SUM ( Sales[Amount] ))",
             "n": 7, "users": 1, "last_ts": day + "T11:00:00", "avg_ms": 60.0,
             "cpu_ms": 150},
        ])


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    tmp = tempfile.mkdtemp(prefix="ctx_selftest_")
    raw, build_dir = os.path.join(tmp, "raw"), os.path.join(tmp, "build")
    out = os.path.join(tmp, "wiki")
    failures = []

    def check(label, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + label + (("  " + str(detail)) if detail else ""))
        if not cond:
            failures.append(label)

    try:
        make_raw(raw)
        import graph
        import parse
        import wiki

        print("parse")
        counts = parse.build(raw, build_dir)
        check("nodes emitted", counts["nodes"] > 30, counts["nodes"])
        check("edges emitted", counts["edges"] > 30, counts["edges"])

        print("graph")
        con, _counts = graph.build(build_dir)     # in memory; nothing lands on disk

        rows = con.execute("SELECT rank, name, owner_item_name, endorsement, views, "
                           "n_reports, conflicting, round(score,2) FROM definitions "
                           "WHERE term_id = 'revenue' ORDER BY rank").fetchall()
        for row in rows:
            print("       " + str(row))
        check("three definitions of revenue", len(rows) == 3, len(rows))
        check("certified model definition ranks first",
              rows and rows[0][1] == "Total Revenue", rows[0] if rows else None)
        check("conflict detected on revenue", rows and rows[0][6] is True)

        ytd = con.execute("SELECT count(*) FROM terms WHERE term_id = 'revenue-ytd'").fetchone()[0]
        check("Revenue YTD is a separate term", ytd == 1, ytd)

        dax = con.execute(
            "SELECT count(*) FROM edges WHERE rel = 'references' "
            "AND json_extract_string(attrs, '$.via') = 'dax' "
            "AND src LIKE 'measure:%'").fetchone()[0]
        check("measure-to-measure DAX reference found", dax >= 1, dax)

        src = con.execute(
            "SELECT dst FROM edges WHERE rel = 'sources_from' AND src LIKE 'model_table:%'"
        ).fetchall()
        bound = [d[0] for d in src if d[0].startswith("lakehouse_table:")]
        check("Direct Lake partition bound to a physical table",
              any("fact_sales" in b for b in bound), bound)
        check("DirectQuery M partition bound too", len(bound) >= 3, bound)

        vis = con.execute("SELECT count(*) FROM edges e JOIN nodes n ON n.id = e.src "
                          "WHERE n.kind = 'visual' AND e.rel = 'references'").fetchone()[0]
        check("visual field references resolved (both formats)", vis >= 3, vis)

        feeds = con.execute("SELECT dst FROM edges WHERE rel = 'feeds' "
                            "AND src = 'notebook:" + NOTEBOOK + "'").fetchall()
        check("notebook write detected", any("fact_sales" in f[0] for f in feeds), feeds)
        reads = con.execute("SELECT dst FROM edges WHERE rel = 'reads' "
                            "AND src = 'notebook:" + NOTEBOOK + "'").fetchall()
        check("notebook reads detected", any("dim_customer" in r[0] for r in reads), reads)

        runs = con.execute("SELECT count(*) FROM edges WHERE rel IN ('runs','refreshes') "
                           "AND src = 'pipeline:" + PIPELINE + "'").fetchone()[0]
        check("pipeline activities parsed", runs == 2, runs)

        views = con.execute("SELECT views FROM item_views WHERE item_id = ?",
                            [REPORT_A]).fetchone()
        check("report views counted", views and views[0] == 36, views)

        top = con.execute("SELECT top_def_id FROM terms WHERE term_id='revenue'").fetchone()[0]
        up = graph.lineage(con, top, "up", 8)
        kinds = {k for _i, k, _n, _w2, _d, _r in up}
        check("lineage reaches the physical table", "lakehouse_table" in kinds, sorted(kinds))
        check("lineage reaches the notebook", "notebook" in kinds, sorted(kinds))
        check("lineage reaches the pipeline", "pipeline" in kinds, sorted(kinds))

        dangling = con.execute(
            "SELECT count(*) FROM edges e LEFT JOIN nodes n ON n.id = e.dst "
            "WHERE n.id IS NULL AND e.dst NOT LIKE 'unresolved:%'").fetchone()[0]
        check("no dangling edges", dangling == 0, dangling)

        avg = con.execute("SELECT rank, name, conflicting FROM definitions "
                          "WHERE term_id = 'avg-price' ORDER BY rank").fetchall()
        check("Average Price and Price_AVG merged into one term",
              len(avg) == 2 and avg[0][2] is True, avg)
        check("certified spelling ranks first", bool(avg) and avg[0][1] == "Price_AVG", avg)
        al = con.execute("SELECT list(alias ORDER BY alias) FROM aliases "
                         "WHERE term_id = 'avg-price'").fetchone()[0]
        check("aliases table has both spellings", al == ["Average Price", "Price_AVG"], al)
        label = con.execute("SELECT label FROM terms WHERE term_id = 'avg-price'").fetchone()[0]
        check("term label is the top definition's name", label == "Price_AVG", label)

        noise = con.execute("SELECT count(*) FROM edges WHERE dst LIKE 'unresolved:table/deltalake%' "
                            "OR dst LIKE 'unresolved:table/psutil%' "
                            "OR dst LIKE 'unresolved:table/shutil%'").fetchone()[0]
        check("python imports are not read as tables", noise == 0, noise)
        check("sql inside a string literal still found",
              any("dim_date" in r[0] for r in reads), reads)

        ext = con.execute("SELECT id FROM nodes WHERE kind = 'lakehouse_table' "
                          "AND TRY_CAST(attrs->>'external' AS BOOLEAN)").fetchall()
        check("external store bound by model B is a stub node",
              any(LH2 in e[0] for e in ext), ext)
        ws2 = con.execute("SELECT attrs->>'workspace_id' FROM nodes WHERE id = ?",
                          ["lakehouse:" + LH2]).fetchone()
        check("stub keeps the workspace id from the OneLake reference",
              ws2 and ws2[0] == WS2, ws2)
        up_ext = graph.lineage(con, "model_table:" + MODEL_B + "/External", "up", 4)
        check("lineage reaches the external table",
              any(row[1] == "lakehouse_table" for row in up_ext), up_ext)

        usage = con.execute("SELECT views, runs, events FROM item_usage WHERE item_id = ?",
                            [MODEL_A]).fetchone()
        check("model usage bucketed, harvest noise excluded", usage == (1, 1, 2), usage)

        qu = dict(con.execute(
            "SELECT split_part(def_id, '/', -1), queries FROM query_usage").fetchall())
        check("query log attributed to the measures the DAX called",
              qu.get("Price_AVG") == 120 and qu.get("Total Revenue") == 15, qu)
        split = con.execute("SELECT report_queries, adhoc_queries FROM query_usage "
                            "WHERE def_id LIKE '%Price_AVG'").fetchone()
        check("report client and hand-written DAX split apart", split == (0, 120), split)
        inline = con.execute("SELECT measureless_queries FROM query_stats "
                             "WHERE item_id = ?", [MODEL_A]).fetchone()[0]
        check("queries that re-derive a measure inline are counted", inline == 21, inline)
        watched = con.execute("SELECT count(*) FROM query_stats WHERE queries = 0").fetchone()[0]
        check("a monitored model nobody queried still gets a row", watched == 1, watched)
        # The point of the signal: a measure no report references, ranked first because
        # people actually ask for it.
        top = con.execute("SELECT name, queries, views FROM definitions "
                          "WHERE term_id = 'avg-price' AND rank = 1").fetchone()
        check("query counts lift a measure no report references",
              top and top[0] == "Price_AVG" and top[1] == 120 and top[2] == 0, top)

        prof = con.execute("SELECT attrs->'profile' FROM nodes WHERE id = ?",
                           ["column:" + MODEL_A + "/Sales/Region"]).fetchone()[0]
        check("lakehouse profile copied onto the model column",
              bool(prof) and '"NSW"' in str(prof), prof)
        n_rows = con.execute("SELECT attrs->>'n_rows' FROM nodes WHERE id = ?",
                             ["lakehouse_table:" + LH + "/dbo/fact_sales"]).fetchone()[0]
        check("lakehouse table carries its row count", n_rows == "1200", n_rows)
        lcol = con.execute("SELECT count(*) FROM nodes WHERE kind = 'column' AND parent_id = ?",
                           ["lakehouse_table:" + LH + "/dbo/fact_sales"]).fetchone()[0]
        check("lakehouse columns emitted as nodes", lcol == 3, lcol)
        built = con.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        check("meta has built_at", bool(built and built[0]), built)

        # the tier: what nothing refers to is demoted, what a model reads is not
        tiers = dict(con.execute(
            "SELECT name, tier FROM nodes WHERE kind = 'lakehouse_table'").fetchall())
        check("a table a model sources from stays tier 1", tiers.get("fact_sales") == 1, tiers)
        check("a table nothing reads is demoted to tier 3",
              tiers.get("scratch_tmp") == 3, tiers)
        endpoints = con.execute("SELECT count(*) FROM nodes WHERE kind = 'sql_endpoint' "
                                "AND tier < 3").fetchone()[0]
        check("sql endpoints are plumbing, never tier 1", endpoints == 0, endpoints)
        check("meta carries the tier split",
              "tiers" in dict(con.execute("SELECT key, value FROM meta").fetchall()))

        print("wiki")
        pages = wiki.render(con, out)
        n_terms = con.execute("SELECT count(*) FROM terms").fetchone()[0]
        check("a page per term", pages.get("term", 0) == n_terms, pages)
        check("a page per model, report, table and notebook",
              pages.get("semantic_model") == 2 and pages.get("report") == 2
              and pages.get("lakehouse_table") == 4 and pages.get("notebook") == 1, pages)
        check("the long tail gets no page of its own", pages.get("tail", 0) >= 1, pages)
        check("no page for the table nothing reads",
              not os.path.exists(os.path.join(out, "tables",
                                              "sales_lh--22222222.dbo.scratch_tmp.md")))
        store_page = open(os.path.join(out, "stores", "sales_lh--22222222.md"),
                          encoding="utf-8").read()
        check("the store page still names it", "`scratch_tmp`" in store_page
              and "Not referenced" in store_page, store_page[-400:])
        broken = wiki.check_links(out)
        check("no broken wikilinks", not broken, broken[:5])
        term_page = os.path.join(out, "terms", "revenue.md")
        text = open(term_page, encoding="utf-8").read()
        check("term page shows the conflict", "disagree" in text)
        check("term page shows DAX", "```dax" in text)
        check("term page lists the report", "Sales Overview" in text)
        check("CLAUDE.md written", os.path.exists(os.path.join(out, "CLAUDE.md")))
        table_page = open(os.path.join(out, "tables", "sales_lh--22222222.dbo.fact_sales.md"),
                          encoding="utf-8").read()
        check("table page lists profiled columns", "| Region | string | 2 |" in table_page)

        # publish: the in-memory build written out as Delta. A real run targets a Fabric
        # lakehouse; here a temp folder stands in for it, so the selftest needs no network.
        import publish
        store = os.path.join(tmp, "context")
        rows = publish.publish(con, store)
        con.close()
        check("Delta tables written through duckrun",
              rows.get("nodes", 0) > 30
              and os.path.isdir(os.path.join(store, "dbo", "nodes", "_delta_log")), rows)

        where = {"workspace": "WS", "workspace_id": "w-1", "lakehouse": "context_layer",
                 "lakehouse_id": "l-1", "path": store}
        loc = os.path.join(tmp, "context.json")
        publish.save_location(where, loc)
        check("the address round-trips through context.json",
              (publish.load_location(loc) or {}).get("path") == store)

        # the harvest side reading back what it published, with no local database left
        back = graph.open_published(store)
        n_back = back.execute("SELECT count(*) FROM definitions WHERE term_id = 'revenue'").fetchone()[0]
        views_back = back.execute("SELECT count(*) FROM flow").fetchone()[0]
        back.close()
        check("harvest side pulls the published context back", n_back == 3 and views_back > 0,
              {"definitions": n_back, "flow": views_back})

        # the Files side: raw/, build/, wiki/ and graph.html kept beside the tables, pushed
        # as a diff. A local folder stands in for the lakehouse, so this needs no network.
        import files as filesmod
        work = os.path.join(tmp, "work")
        os.makedirs(os.path.join(work, "build"), exist_ok=True)
        shutil.copy(os.path.join(build_dir, "nodes.jsonl"),
                    os.path.join(work, "build", "nodes.jsonl"))
        shutil.copytree(out, os.path.join(work, "wiki"))
        first = filesmod.push(store, work, ["build", "wiki"], log=lambda _m: None)
        check("files pushed to the store",
              first["build"]["sent"] == 1 and first["wiki"]["sent"] > 10
              and os.path.isfile(os.path.join(store, "build", "nodes.jsonl")), first)

        again = filesmod.push(store, work, ["build", "wiki"], log=lambda _m: None)
        check("a second push sends nothing", again["build"]["sent"] == 0
              and again["wiki"]["sent"] == 0 and again["wiki"]["skipped"] > 10, again)

        gone = os.path.join(work, "build", "nodes.jsonl")
        os.remove(gone)
        after = filesmod.push(store, work, ["build"], log=lambda _m: None)
        check("a deleted file is deleted remotely", after["build"]["deleted"] == 1
              and not os.path.isfile(os.path.join(store, "build", "nodes.jsonl")), after)

        back = os.path.join(tmp, "work2")
        filesmod.pull(store, back, ["wiki"], log=lambda _m: None)
        check("files pull brings them back",
              len(filesmod._walk(os.path.join(back, "wiki"))) > 10,
              len(filesmod._walk(os.path.join(back, "wiki"))))


        # the nightly refresh notebook: valid ipynb, pure Python, config baked in, and the
        # code it stages is the harvest and not the query side
        import deploy as deploymod
        nb_path = os.path.join(tmp, "context_refresh.ipynb")
        deploymod.build_ipynb({"store": "ws/lh", "lakehouse": "WS/context_layer",
                               "workspaces": ["WS"], "days": 28, "stale_after_days": 1.0,
                               "profile": True, "wiki": True}, nb_path)
        book = json.loads(open(nb_path, encoding="utf-8").read())
        meta = book.get("metadata", {})
        srcs = ["".join(c["source"]) for c in book["cells"]]
        check("notebook is valid pure-Python ipynb",
              book.get("nbformat") == 4
              and meta.get("kernel_info", {}).get("name") == "jupyter"
              and meta.get("language_info", {}).get("name") == "python"
              and all(isinstance(c["source"], list) for c in book["cells"]),
              {"kernel": meta.get("kernel_info"), "cells": len(book["cells"])})
        check("first cell installs duckrun and restarts, alone",
              srcs[0].startswith("!pip install") and "restartPython()" in srcs[0]
              and "import" not in srcs[0], srcs[0])
        check("config is baked into the notebook",
              "'workspaces'" in srcs[1] and "'WS'" in srcs[1] and "context_layer" in srcs[1])
        # Every cell must be Python. json.dumps writes true/false/null, which are three
        # NameErrors in a notebook, and the job's only error is "statements failed".
        bad = []
        for i, src in enumerate(srcs):
            if src.lstrip().startswith("!"):
                continue                               # a shell escape, not Python
            try:
                compile(src, "<cell " + str(i) + ">", "exec")
            except SyntaxError as exc:
                bad.append(str(i) + ": " + str(exc)[:80])
        check("every notebook cell compiles as Python", not bad, bad)
        ns = {}
        exec(srcs[1], ns)
        check("the config cell evaluates to the config",
              ns["CONFIG"]["workspaces"] == ["WS"] and ns["CONFIG"]["profile"] is True,
              ns.get("CONFIG"))
        shipped = {os.path.basename(p) for p in deploymod.code_files()}
        check("the notebook ships the harvest, not the runner or the query side",
              {"harvest.py", "graph.py", "publish.py", "files.py", "schema.sql"} <= shipped
              and not {"run.py", "selftest.py", "deploy.py"} & shipped, sorted(shipped))

        # the query side, on the published tables alone, through duckrun
        sys.path.insert(0, os.path.dirname(here))
        from ask import context as askctx
        qcon = askctx.open_db(os.path.join(tmp, "context"))
        contract = askctx.contract(qcon, os.path.join(tmp, "context"))
        check("query side: contract satisfied by the published tables", contract["ok"],
              {k: v for k, v in contract["required"].items() if not v["present"]})
        hits = askctx.search(qcon, "average price")["hits"]
        check("query side: search finds the merged term",
              bool(hits) and hits[0]["term_id"] == "avg-price", hits[:2])
        defn = askctx.define(qcon, "Average Price")
        check("query side: define ranks the certified model first",
              defn["conflicting"] and defn["definitions"][0]["name"] == "Price_AVG"
              and defn["definitions"][0]["model"]["workspace_id"] == WS, defn["definitions"][0]["model"])
        pack = askctx.model(qcon, "Sales Model")
        region = next((c for t in pack["tables"] if t["name"] == "Sales"
                       for c in t["columns"] if c["name"] == "Region"), {})
        check("query side: model pack carries relationships and profiled values",
              pack["relationships"] and region.get("profile", {}).get("values") == ["NSW", "VIC"],
              region)
        up = askctx.lineage(qcon, "Sales Model")["nodes"]
        check("query side: lineage walks to the lakehouse table",
              any(n["kind"] == "lakehouse_table" for n in up), [n["kind"] for n in up][:6])
        raw = askctx.raw_sql(qcon, "select count(*) from terms")
        check("query side: raw sql over the context", raw["rows"][0] and list(raw["rows"][0].values())[0] >= 3, raw)
        try:
            askctx.raw_sql(qcon, "delete from nodes")
            refused_sql = False
        except askctx.NotFound:
            refused_sql = True
        check("query side: raw sql refuses writes", refused_sql)
        qcon.close()

        print("\n--- terms/revenue.md ---")
        print(text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(("FAILED: " + ", ".join(failures)) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
