"""Live calls to Fabric - the only network code on the query side.

Two things, both read-only and both DAX: run a query on a semantic model, and list the
values of a column when the harvest carries no profile for it. Tokens and HTTP come from
duckrun, the same library the harvest uses, but nothing here touches src/ or the raw/
folder. Numbers about the data only ever come from here.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

PBI_API = "https://api.powerbi.com/v1.0/myorg"
MAX_ROWS_DEFAULT = 100
MAX_ROWS_HARD = 10000

_COMMENT = re.compile(r"/\*.*?\*/|(?://|--)[^\n]*", re.S)
class Refused(Exception):
    """The query is not the read-only kind this side runs."""


class RemoteError(Exception):
    """Fabric said no; the message carries what it said and what to check."""


def _first_token(query: str) -> str:
    body = _COMMENT.sub(" ", query or "").strip()
    return re.split(r"[\s(]+", body, 1)[0].upper() if body else ""


def dax_is_query(query: str) -> bool:
    return _first_token(query) in ("EVALUATE", "DEFINE")


def _dax_name(table: str, column: str) -> str:
    return "'" + table.replace("'", "''") + "'[" + column.replace("]", "]]") + "]"


# ---------------------------------------------------------------- DAX

def _error_text(resp) -> str:
    text = ""
    try:
        err = (resp.json() or {}).get("error") or {}
        details = ((err.get("pbi.error") or {}).get("details") or [])
        parts = [str((d.get("detail") or {}).get("value") or "") for d in details]
        text = "; ".join(p for p in parts if p) or str(err.get("message") or err.get("code") or "")
    except Exception:                                  # noqa: BLE001 - not JSON
        text = (resp.text or "")[:300]
    hint = ""
    if resp.status_code in (401, 403):
        hint = (" Check: the token has the Power BI scope (az login), the tenant setting "
                "'Dataset Execute Queries REST API' is on, and you have Build permission "
                "on the model.")
    elif resp.status_code == 404:
        hint = " Check the workspace and dataset ids (python -m ask scope)."
    return "HTTP " + str(resp.status_code) + ": " + (text or "no detail") + hint


def dax(workspace_id: str, dataset_id: str, query: str, max_rows: int = MAX_ROWS_DEFAULT,
        timeout: int = 180) -> Dict[str, Any]:
    """Run one DAX query through executeQueries and return its first table."""
    if not dax_is_query(query):
        raise Refused("only EVALUATE / DEFINE queries run here; got: "
                      + (query or "").strip()[:60])
    from duckrun.auth import get_powerbi_token
    from duckrun.fabric_remote import _http_request

    max_rows = max(1, min(int(max_rows or MAX_ROWS_DEFAULT), MAX_ROWS_HARD))
    url = PBI_API + "/groups/" + workspace_id + "/datasets/" + dataset_id + "/executeQueries"
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    started = time.time()
    resp = _http_request("POST", url, token=get_powerbi_token(), json_body=body,
                         timeout=timeout)
    elapsed = int((time.time() - started) * 1000)
    if resp.status_code >= 400:
        raise RemoteError(_error_text(resp))
    payload = resp.json() or {}
    result = (payload.get("results") or [{}])[0]
    if result.get("error"):
        raise RemoteError(str(result["error"]))
    tables = result.get("tables") or []
    rows = (tables[0].get("rows") or []) if tables else []
    columns = list(rows[0].keys()) if rows else []
    return {"query": query, "workspace_id": workspace_id, "dataset_id": dataset_id,
            "columns": columns, "rows": rows[:max_rows], "row_count": len(rows),
            "truncated": len(rows) > max_rows, "elapsed_ms": elapsed}


def values(workspace_id: str, dataset_id: str, table: str, column: str,
           limit: int = 50) -> Dict[str, Any]:
    """Distinct values, min, max and distinct count of one model column, live."""
    ref = _dax_name(table, column)
    listing = dax(workspace_id, dataset_id,
                  "EVALUATE TOPN(" + str(limit + 1) + ", VALUES(" + ref + "))", limit + 1)
    vals = [next(iter(r.values())) for r in listing["rows"]]
    vals = [v for v in vals if v is not None]
    bounds = dax(workspace_id, dataset_id,
                 'EVALUATE ROW("min", MIN(' + ref + '), "max", MAX(' + ref + '), '
                 '"n", DISTINCTCOUNT(' + ref + "))", 1)
    row = bounds["rows"][0] if bounds["rows"] else {}
    return {"table": table, "column": column, "source": "live",
            "values": sorted(vals, key=str)[:limit], "truncated": len(vals) > limit,
            "min": row.get("[min]"), "max": row.get("[max]"), "n_distinct": row.get("[n]"),
            "elapsed_ms": listing["elapsed_ms"] + bounds["elapsed_ms"]}


# ---------------------------------------------------------------- SQL analytics endpoint

# A lakehouse or warehouse answers T-SQL over TDS on its workspace's SQL analytics endpoint.
# DuckDB's mssql community extension speaks TDS directly and takes an Entra access token, so
# a store attaches into the same connection the context tables live in - a join across the
# two is one query - and no ODBC driver has to be installed.
SQL_SCOPE = "https://database.windows.net/.default"
MSSQL_EXTENSION = "mssql"

_ENDPOINTS: Dict[str, str] = {}
_SQL_TOKEN: List[Any] = []


class NoSqlEndpoint(Exception):
    """The store has no reachable SQL analytics endpoint, or the extension is missing."""


def sql_endpoint(workspace_id: str, item_id: str, kind: str = "lakehouse") -> str:
    """The TDS hostname serving a lakehouse's or warehouse's SQL analytics endpoint.

    Live, because the harvest records the endpoint's item id but not its hostname, and the
    hostname is what a connection needs. Cached per process - it does not move.
    """
    key = str(workspace_id) + "/" + str(item_id)
    if key in _ENDPOINTS:
        return _ENDPOINTS[key]
    from duckrun.auth import get_fabric_token
    from duckrun.fabric_remote import _http_request

    coll = "warehouses" if kind == "warehouse" else "lakehouses"
    url = ("https://api.fabric.microsoft.com/v1/workspaces/" + str(workspace_id) + "/"
           + coll + "/" + str(item_id))
    resp = _http_request("GET", url, token=get_fabric_token())
    if resp.status_code >= 400:
        raise NoSqlEndpoint("HTTP " + str(resp.status_code) + " reading " + coll[:-1]
                            + " " + str(item_id) + ": " + (resp.text or "")[:200])
    props = resp.json().get("properties") or {}
    host = props.get("connectionString")
    if not host:                                       # a lakehouse nests it one level down
        sqlprops = props.get("sqlEndpointProperties") or {}
        host = sqlprops.get("connectionString")
        status = sqlprops.get("provisioningStatus")
        if not host and status and status != "Success":
            raise NoSqlEndpoint("the SQL endpoint for " + str(item_id) + " is "
                                + str(status) + ", not ready to query")
    if not host:
        raise NoSqlEndpoint("no SQL analytics endpoint on " + coll[:-1] + " " + str(item_id))
    _ENDPOINTS[key] = host
    return host


def sql_token() -> str:
    """An Entra token for the SQL endpoint - a fourth audience, so duckrun's three named
    entry points do not cover it."""
    if _SQL_TOKEN:
        return _SQL_TOKEN[0]
    try:
        import notebookutils                           # noqa: F401 - Fabric runtime only
        token = notebookutils.credentials.getToken(SQL_SCOPE)
    except Exception:                                  # noqa: BLE001 - not in a notebook
        from duckrun.auth import _azure_identity_token
        token = _azure_identity_token(SQL_SCOPE)
    if not token:
        raise NoSqlEndpoint("no token for the SQL endpoint; run "
                            "`az login --scope " + SQL_SCOPE + "`")
    _SQL_TOKEN.append(token)
    return token


def attach_store(con, alias: str, database: str, host: str) -> str:
    """ATTACH a lakehouse or warehouse read-only into an open DuckDB connection.

    Returns the catalog alias the query should qualify its tables with. Idempotent: a store
    already attached on this connection is left alone.
    """
    attached = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    if alias in attached:
        return alias
    try:
        con.execute("INSTALL " + MSSQL_EXTENSION + " FROM community")
        con.execute("LOAD " + MSSQL_EXTENSION)
    except Exception as exc:                           # noqa: BLE001
        raise NoSqlEndpoint(
            "the DuckDB '" + MSSQL_EXTENSION + "' community extension is needed to reach a "
            "SQL endpoint and would not load: " + str(exc)[:200])
    secret = "ctx_" + re.sub(r"[^0-9A-Za-z_]", "_", alias)
    con.execute("CREATE OR REPLACE SECRET " + secret + " (TYPE mssql, PROVIDER config, "
                "host $host, database $db, access_token $tok)",
                {"host": host, "db": database, "tok": sql_token()})
    # ATTACH takes no prepared parameter for its target, so the spec is inlined; the
    # database is a Fabric display name, quoted the SQL way rather than trusted.
    con.execute("ATTACH " + _quote_str("database=" + database) + " AS " + _quote_ident(alias)
                + " (TYPE mssql, SECRET " + secret + ", READ_ONLY)")
    return alias


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _quote_str(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"
