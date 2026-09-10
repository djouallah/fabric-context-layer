"""Fabric / Power BI REST calls, wrapped thin over duckrun.

duckrun already solves the three hard parts - token acquisition for three different
audiences, 429/5xx retry with Retry-After, and the 202 long-running-operation dance that
getDefinition uses - so nothing here re-implements them.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from duckrun.auth import get_fabric_token, get_onelake_token, get_powerbi_token
from duckrun.fabric_remote import _get_definition, _http_request, _paged_values

FABRIC_API = "https://api.fabric.microsoft.com/v1"
PBI_API = "https://api.powerbi.com/v1.0/myorg"

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
    return get_fabric_token()


def pbi_token() -> str:
    return get_powerbi_token()


def onelake_token() -> str:
    return get_onelake_token()


def _paged(url: str, token: str, list_key: str, params: Optional[dict] = None) -> List[Dict]:
    """Every row across all pages of an endpoint whose body is {<list_key>: [...]}.

    duckrun's _paged_values only understands bodies keyed 'value'; the admin, scanner and
    lakehouse-tables endpoints each use a different key, so this is the general form.
    """
    out: List[Dict] = []
    while True:
        resp = _http_request("GET", url, token=token, params=params)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get(list_key) or [])
        next_uri = body.get("continuationUri")
        next_tok = body.get("continuationToken")
        if next_uri and body.get("lastResultSet") is not True:
            url, params = next_uri, None
        elif next_tok:
            params = dict(params or {})
            params["continuationToken"] = next_tok
        else:
            return out


# ---------------------------------------------------------------- inventory

def workspace_items(ws_id: str, token: str) -> List[Dict]:
    """Every item in the workspace, each tagged with its type."""
    return _paged_values(FABRIC_API + "/workspaces/" + ws_id + "/items", token=token)


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
    from dbt.adapters.duckrun.remote import list_delta_tables

    root = ("abfss://" + ws_id + "@onelake.dfs.fabric.microsoft.com/" + item_id + "/Tables")
    so = {"bearer_token": token}
    top = list_delta_tables(root, "", so)
    out: List[Dict] = []
    for name in top:
        children = []
        try:
            children = list_delta_tables(root, name, so)
        except Exception:                            # noqa: BLE001 - a table, not a schema
            children = []
        if children:
            out.extend({"schema": name, "name": child} for child in children)
        else:
            out.append({"schema": "dbo", "name": name})
    return out


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

    The audience is the cluster itself, which is neither of duckrun's three - inside a
    Fabric notebook notebookutils mints it directly, and locally azure-identity does.
    """
    try:
        import notebookutils                          # noqa: F401 - Fabric runtime only
        return notebookutils.credentials.getToken(cluster_uri)
    except Exception:                                 # noqa: BLE001 - not in a notebook
        pass
    from duckrun.auth import _azure_identity_token
    token = _azure_identity_token(cluster_uri.rstrip("/") + "/.default")
    if not token:
        raise RuntimeError(
            "no token for " + cluster_uri + "; run `az login --scope "
            + cluster_uri.rstrip("/") + "/.default`")
    return token


def _items_of_type(ws_id: str, item_type: str, token: str) -> List[Dict]:
    return _paged_values(FABRIC_API + "/workspaces/" + ws_id + "/items", token=token,
                         params={"type": item_type})


def monitoring_database(ws_id: str, token: str) -> Optional[Dict]:
    """{cluster, database, item_id} for the workspace's monitoring Eventhouse, or None.

    Workspace monitoring creates a read-only Eventhouse holding SemanticModelLogs. The
    typed collections are tried first because they carry queryServiceUri; the generic item
    list is the fallback for a tenant where the monitoring item is hidden from them.
    """
    for coll in ("kqlDatabases", "eventhouses"):
        try:
            rows = _paged_values(FABRIC_API + "/workspaces/" + ws_id + "/" + coll, token=token)
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
