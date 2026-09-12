"""The Fabric / Power BI REST plumbing: one retrying call, one pager, one long-running
operation poller.

Three things make these APIs awkward and all three are handled here so nothing above has to
think about them:

- **Throttling.** 429 and the transient 5xx are retried, honouring a numeric `Retry-After`.
  A single throttle must not kill a harvest that is halfway through a tenant.
- **Pagination.** Fabric caps every list response and points at the rest through either a
  `continuationUri` or a `continuationToken`, and the list itself sits under a different key
  per endpoint - `value`, `itemEntities`, `data`, `activityEventEntities`. Reading only the
  first page silently returns a fraction of a large workspace.
- **Long-running operations.** `getDefinition` and item creation answer 202 with a
  `Location` to poll, then serve the payload from a `/result` sub-url.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

# Transient statuses worth retrying. Everything else - 403, 404, a bad request - propagates
# immediately, because retrying it only delays the error.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 600.0

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_SESSION = None


class FabricError(RuntimeError):
    """A Fabric API call that failed in a way the caller cannot paper over."""


def looks_like_guid(value) -> bool:
    return bool(_GUID.match(str(value or "")))


def _session():
    """One shared `requests.Session`. A harvest makes thousands of calls to the same two
    hosts; `requests.get` would build and tear down a TLS connection for each."""
    global _SESSION
    if _SESSION is None:
        import requests
        _SESSION = requests.Session()
    return _SESSION


def _sleep(seconds: float) -> None:
    """Indirection so a test can stub the wait."""
    time.sleep(seconds)


def _delay(resp, attempt: int) -> float:
    after = resp.headers.get("Retry-After")
    if after and str(after).strip().isdigit():
        return float(after)
    return float(2 ** attempt)


def request(method: str, url: str, *, token: str, params: Optional[dict] = None,
            json_body: Optional[dict] = None, headers: Optional[dict] = None,
            timeout: int = 60):
    """A bounded-retry HTTP call returning the `requests.Response`.

    The last attempt's response is returned even when it is still transient, so the caller's
    `raise_for_status()` fails loudly rather than this swallowing it.
    """
    hdrs = {"Authorization": "Bearer " + token}
    if json_body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    kwargs = {"params": params, "headers": hdrs, "timeout": timeout}
    if json_body is not None:
        kwargs["json"] = json_body
    call = getattr(_session(), method.lower())
    resp = None
    for attempt in range(MAX_ATTEMPTS):
        resp = call(url, **kwargs)
        if resp.status_code not in RETRY_STATUS:
            return resp
        if attempt < MAX_ATTEMPTS - 1:
            _sleep(_delay(resp, attempt))
    return resp


def paged(url: str, token: str, list_key: str = "value",
          params: Optional[dict] = None) -> List[Dict]:
    """Every row across every page of an endpoint whose body is `{<list_key>: [...]}`."""
    out: List[Dict] = []
    while True:
        resp = request("GET", url, token=token, params=params)
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


def await_lro_result(token: str, resp) -> dict:
    """Poll a long-running operation to Succeeded and return its `/result` body."""
    location = resp.headers.get("Location")
    if not location:
        raise FabricError("operation returned no Location to poll")
    for _ in range(int(POLL_TIMEOUT // max(POLL_INTERVAL, 1)) + 1):
        _sleep(POLL_INTERVAL)
        poll = request("GET", location, token=token)
        poll.raise_for_status()
        status = poll.json().get("status")
        if status == "Succeeded":
            result = request("GET", location.rstrip("/") + "/result", token=token)
            result.raise_for_status()
            return result.json()
        if status in ("Failed", "Undetermined"):
            raise FabricError("operation failed: " + str(poll.json())[:300])
    raise FabricError("timed out waiting for the operation result")


def await_lro_item_id(token: str, resp) -> str:
    """Poll a create operation to completion and return the new item's id. Some tenants put
    it on the operation itself, others only on `/result`."""
    location = resp.headers.get("Location")
    if not location:
        raise FabricError("create returned no Location to poll")
    for _ in range(int(POLL_TIMEOUT // max(POLL_INTERVAL, 1)) + 1):
        _sleep(POLL_INTERVAL)
        poll = request("GET", location, token=token)
        poll.raise_for_status()
        body = poll.json()
        status = body.get("status")
        if status == "Succeeded":
            if body.get("id"):
                return body["id"]
            result = request("GET", location.rstrip("/") + "/result", token=token)
            result.raise_for_status()
            return result.json()["id"]
        if status in ("Failed", "Undetermined"):
            raise FabricError("item create failed: " + str(body)[:300])
    raise FabricError("timed out creating the item")


def get_definition(token: str, ws_id: str, endpoint: str, item_id: str,
                   fmt: Optional[str] = None) -> List[dict]:
    """An item's definition parts, `[{"path", "payload", "payloadType"}, ...]`.

    `fmt` is the definition format for the types that have one to ask for - TMSL for a
    semantic model, PBIR or PBIR-Legacy for a report, ipynb for a notebook.
    """
    resp = request("POST", FABRIC_API + "/workspaces/" + ws_id + "/" + endpoint + "/"
                   + item_id + "/getDefinition", token=token,
                   params={"format": fmt} if fmt else None)
    if resp.status_code == 200:
        body = resp.json()
    elif resp.status_code == 202:
        body = await_lro_result(token, resp)
    else:
        raise FabricError("could not read " + endpoint + " " + item_id + " definition (HTTP "
                          + str(resp.status_code) + "): " + resp.text[:300])
    return body["definition"]["parts"]
