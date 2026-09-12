"""Live calls to Fabric - the only network code on the query side, and the only way here
to learn a number.

One thing, read-only: run a DAX query on a semantic model. The model's workspace and item
ids come from `context.md`, so no lookup precedes the call. Tokens and HTTP come from
fabcontext's Fabric layer, the same one the harvest uses.

There is no SQL path. A table no semantic model covers has no agreed definition behind it,
and inventing one in SQL is the thing the context layer exists to stop - so such a question
is answered by saying that, not by computing it.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

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
        hint = " Check the workspace and model ids - context.md prints both."
    return "HTTP " + str(resp.status_code) + ": " + (text or "no detail") + hint


def dax(workspace_id: str, dataset_id: str, query: str, max_rows: int = MAX_ROWS_DEFAULT,
        timeout: int = 180) -> Dict[str, Any]:
    """Run one DAX query through executeQueries and return its first table."""
    if not dax_is_query(query):
        raise Refused("only EVALUATE / DEFINE queries run here; got: "
                      + (query or "").strip()[:60])
    from fabcontext._fabric import auth
    from fabcontext._fabric.rest import request as _http_request

    max_rows = max(1, min(int(max_rows or MAX_ROWS_DEFAULT), MAX_ROWS_HARD))
    url = PBI_API + "/groups/" + workspace_id + "/datasets/" + dataset_id + "/executeQueries"
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    started = time.time()
    resp = _http_request("POST", url, token=auth.powerbi_token(), json_body=body,
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
