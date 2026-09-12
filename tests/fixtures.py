"""A synthetic tenant, as a folder of harvested JSON.

One workspace holding everything the parsers have to cope with: a Direct Lake model, a second
model whose DAX for the same term disagrees, a DirectQuery model bound to a store nobody
harvested, a PBIR report, a legacy report, a notebook, a pipeline, a scanner result, a profile
file, 28 days of activity and a query log.

This is what makes the suite offline. Nothing here is a mock - it is the real shape of a
harvest, so everything downstream of the fetch runs for real against it.
"""
from __future__ import annotations

import datetime as dt
import json
import os

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
