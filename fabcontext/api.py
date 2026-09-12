"""The Fabric and Power BI REST calls the harvest makes, and nothing else.

One call per thing the harvest wants to know. The three awkward parts - a token per
audience, retrying through throttling, and the 202 long-running-operation dance that
getDefinition uses - all live in `_fabric`, so this file reads as a list of endpoints.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ._fabric import auth
from ._fabric.rest import FABRIC_API, POWERBI_API as PBI_API
from ._fabric.rest import get_definition as _get_definition
from ._fabric.rest import paged as _paged
from ._fabric.rest import request as _http_request

# getDefinition is a long-running operation; a workspace with hundreds of items would
# otherwise walk straight into the throttle.
PACE_SECONDS = 0.2

# REST collection and definition format per Fabric item type. Types absent here have no
# definition endpoint (lakehouse, warehouse, SQL endpoint, environment, KQL...) and are
# harvested as inventory only.
ITEM_ENDPOINT = {
    "SemanticModel": "semanticModels",
    "Report": "reports",
    "Notebook": "notebooks",
    "DataPipeline": "dataPipelines",
    "Dataflow": "dataflows",
    "VariableLibrary": "variableLibraries",
}
# Formats to try in order; the first that returns parts wins. Reports moved from a single
# report.json (PBIR-Legacy) to a folder of page/visual json (PBIR), and which one a report
# is stored as depends on when it was last saved.
ITEM_FORMATS = {
    "SemanticModel": ["TMSL"],
    "Report": ["PBIR", "PBIR-Legacy"],
    "Notebook": ["ipynb"],
    "DataPipeline": [None],
    "Dataflow": [None],
    "VariableLibrary": [None],
}


def fabric_token() -> str:
    return auth.fabric_token()


def pbi_token() -> str:
    return auth.powerbi_token()


def onelake_token() -> str:
    return auth.onelake_token()


# ---------------------------------------------------------------- inventory

def workspace_items(ws_id: str, token: str) -> List[Dict]:
    """Every item in the workspace, each tagged with its type."""
    return _paged(FABRIC_API + "/workspaces/" + ws_id + "/items", token)


def workspace_info(ws_id: str, token: str) -> Dict:
    resp = _http_request("GET", FABRIC_API + "/workspaces/" + ws_id, token=token)
    resp.raise_for_status()
    return resp.json()


def admin_items(ws_id: str, token: str) -> List[Dict]:
    """The admin view of the same items - the only place lastUpdatedDate and the creator
    are exposed for EVERY item type. Needs Fabric admin; callers treat failure as optional.
    """
    return _paged(FABRIC_API + "/admin/items", token, "itemEntities",
                  params={"workspaceId": ws_id})


# ---------------------------------------------------------------- definitions

def get_definition(ws_id: str, item_type: str, item_id: str, token: str):
    """(format, parts) for an item, trying each candidate format in turn.

    parts are [{"path", "payload" (base64), "payloadType"}]. Returns (None, None) when the
    type has no definition endpoint or every format was refused.
    """
    endpoint = ITEM_ENDPOINT.get(item_type)
    if not endpoint:
        return None, None
    last_error = None
    for fmt in ITEM_FORMATS.get(item_type, [None]):
        try:
            parts = _get_definition(token, ws_id, endpoint, item_id, fmt=fmt)
            time.sleep(PACE_SECONDS)
            if parts:
                return fmt or "default", parts
        except Exception as exc:                     # noqa: BLE001 - format probe
            last_error = exc
            time.sleep(PACE_SECONDS)
    if last_error is not None:
        raise last_error
    return None, None


# ---------------------------------------------------------------- lakehouse / warehouse

def lakehouse(ws_id: str, item_id: str, token: str) -> Dict:
    resp = _http_request("GET", FABRIC_API + "/workspaces/" + ws_id + "/lakehouses/" + item_id,
                         token=token)
    resp.raise_for_status()
    return resp.json()


def lakehouse_tables(ws_id: str, item_id: str, token: str) -> List[Dict]:
    """[{name, type, format, location}] via the REST tables endpoint.

    Raises for a schema-enabled lakehouse, which the endpoint refuses - the caller falls
    back to listing OneLake directly.
    """
    return _paged(FABRIC_API + "/workspaces/" + ws_id + "/lakehouses/" + item_id + "/tables",
                  token, "data", params={"maxResults": 100})


def warehouse(ws_id: str, item_id: str, token: str) -> Dict:
    resp = _http_request("GET", FABRIC_API + "/workspaces/" + ws_id + "/warehouses/" + item_id,
                         token=token)
    resp.raise_for_status()
    return resp.json()


def onelake_tables(ws_id: str, item_id: str, token: str) -> List[Dict]:
    """Tables discovered by listing the item's OneLake Tables/ folder - the fallback for a
    schema-enabled lakehouse and the only route for a warehouse.

    Returns [{schema, name}]. A schema-enabled store has one directory level of schemas
    above the tables; an unschematised one has the tables directly.
    """
    from ._fabric.onelake import OneLakeStore

    return OneLakeStore(ws_id, item_id, token=token).list_table_dirs()


# ---------------------------------------------------------------- scanner

def scan(ws_ids: List[str], token: str, poll_seconds: float = 5.0,
         timeout_seconds: float = 900.0) -> Dict:
    """Run the Power BI Scanner API over up to 100 workspaces and return the scan result.

    This is the only source for endorsement, dataset->report binding, upstream dataflows,
    datasource instances and cross-workspace lineage. Needs Fabric admin plus the tenant
    settings that enhance admin API responses with detailed metadata and with DAX/mashup
    expressions - without the second, measure expressions come back null.
    """
    resp = _http_request(
        "POST", PBI_API + "/admin/workspaces/getInfo", token=token,
        params={"lineage": "True", "datasourceDetails": "True", "datasetSchema": "True",
                "datasetExpressions": "True", "getArtifactUsers": "True"},
        json_body={"workspaces": list(ws_ids)[:100]})
    resp.raise_for_status()
    scan_id = resp.json()["id"]
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(poll_seconds)
        status = _http_request("GET", PBI_API + "/admin/workspaces/scanStatus/" + scan_id,
                               token=token)
        status.raise_for_status()
        state = status.json().get("status")
        if state == "Succeeded":
            result = _http_request("GET", PBI_API + "/admin/workspaces/scanResult/" + scan_id,
                                   token=token)
            result.raise_for_status()
            return result.json()
        if state in ("Failed", "Undetermined"):
            raise RuntimeError("scan failed: " + str(status.json()))
    raise RuntimeError("scan timed out after " + str(timeout_seconds) + "s")


# ---------------------------------------------------------------- activity

def activity_events(day: str, token: str) -> List[Dict]:
    """Every audit event for one UTC day (YYYY-MM-DD).

    The endpoint takes exactly one day per call and wants the timestamps wrapped in literal
    single quotes. Retention is 30 days. Duplicate event ids appear across pages, so the
    caller de-duplicates on Id.
    """
    params = {"startDateTime": "'" + day + "T00:00:00.000Z'",
              "endDateTime": "'" + day + "T23:59:59.999Z'"}
    rows = _paged(PBI_API + "/admin/activityevents", token, "activityEventEntities", params)
    seen = set()
    out = []
    for row in rows:
        key = row.get("Id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


# ---------------------------------------------------------------- workspace monitoring

MONITORING_DB_HINTS = ("monitoring", "workspacemonitoring")


def kusto_token(cluster_uri: str) -> str:
    """A bearer token for an Eventhouse's query endpoint.

    The audience is the cluster itself, so this is a fourth one - not interchangeable with
    the storage, Fabric or Power BI tokens.
    """
    return auth.kusto_token(cluster_uri)


def _items_of_type(ws_id: str, item_type: str, token: str) -> List[Dict]:
    return _paged(FABRIC_API + "/workspaces/" + ws_id + "/items", token,
                  params={"type": item_type})


def monitoring_database(ws_id: str, token: str) -> Optional[Dict]:
    """{cluster, database, item_id} for the workspace's monitoring Eventhouse, or None.

    Workspace monitoring creates a read-only Eventhouse holding SemanticModelLogs. The
    typed collections are tried first because they carry queryServiceUri; the generic item
    list is the fallback for a tenant where the monitoring item is hidden from them.
    """
    for coll in ("kqlDatabases", "eventhouses"):
        try:
            rows = _paged(FABRIC_API + "/workspaces/" + ws_id + "/" + coll, token)
        except Exception:                             # noqa: BLE001 - collection may be refused
            continue
        for row in rows:
            props = row.get("properties") or {}
            uri = props.get("queryServiceUri")
            if not uri:
                continue
            # Kusto addresses the database by the Fabric ITEM ID, not the display name -
            # asking for "Monitoring KQL database" comes back EntityNotFound.
            db_id = (row.get("id") if coll == "kqlDatabases"
                     else next(iter(props.get("databasesItemIds") or []), None))
            if not db_id:
                continue
            return {"cluster": uri, "database": db_id, "item_id": db_id,
                    "display": row.get("displayName") or ""}
    for item_type in ("KQLDatabase", "Eventhouse"):
        try:
            rows = _items_of_type(ws_id, item_type, token)
        except Exception:                             # noqa: BLE001
            continue
        for row in rows:
            slug = (row.get("displayName") or "").replace(" ", "").lower()
            if any(hint in slug for hint in MONITORING_DB_HINTS):
                return {"cluster": None, "database": row.get("id"),
                        "item_id": row.get("id"), "display": row.get("displayName") or ""}
    return None


def kql_query(cluster: str, database: str, kql: str, token: str,
              timeout_seconds: float = 240.0) -> List[Dict]:
    """Run KQL against an Eventhouse and return the primary result as a list of dicts."""
    resp = _http_request("POST", cluster.rstrip("/") + "/v1/rest/query", token=token,
                         json_body={"db": database, "csl": kql},
                         headers={"Accept": "application/json"},
                         timeout=int(timeout_seconds))
    resp.raise_for_status()
    tables = resp.json().get("Tables") or []
    if not tables:
        return []
    primary = tables[0]
    names = [c.get("ColumnName") for c in primary.get("Columns") or []]
    return [dict(zip(names, row)) for row in primary.get("Rows") or []]
