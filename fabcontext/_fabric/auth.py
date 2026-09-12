"""Bearer tokens, one function per audience.

Five audiences are in play and they are genuinely different: OneLake storage, the Fabric
control plane, the Power BI REST API, an Eventhouse cluster, and the SQL endpoint. A token
for one 401s on another, so nothing here is interchangeable.

Acquisition order, cheapest first:

1. **inside a Fabric notebook** - `notebookutils.credentials.getToken`, which is the only
   path that matters in production and needs no sign-in at all;
2. an already-minted token in the environment;
3. **azure-identity** - Azure CLI, then an interactive browser but only on a TTY, so a
   headless run can never hang waiting for a redirect that will not come.

Tokens are cached per (tenant, scope) and re-acquired near expiry, read out of the JWT
itself. A failed re-acquire keeps the token it has rather than raising over one that is
merely inside the refresh margin.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from typing import Callable, Dict, Optional

STORAGE_SCOPE = "https://storage.azure.com/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
SQL_SCOPE = "https://database.windows.net/.default"

# Env vars honoured per scope, so a CI run can inject a token instead of signing in.
_ENV = {STORAGE_SCOPE: "AZURE_STORAGE_TOKEN", FABRIC_SCOPE: "FABRIC_TOKEN",
        POWERBI_SCOPE: "POWERBI_TOKEN", SQL_SCOPE: "SQL_TOKEN"}

_CACHE: Dict[tuple, str] = {}
_LOCK = threading.RLock()
_EXPIRY: Dict[str, Optional[float]] = {}


def _notebook_token(audience: str) -> Optional[str]:
    """A token from the Fabric notebook runtime, or None when not running in one."""
    try:
        import notebookutils                        # noqa: F401 - Fabric runtime only
    except ImportError:
        return None
    try:
        return notebookutils.credentials.getToken(audience) or None
    except Exception:                               # noqa: BLE001 - audience may be unknown
        return None


def azure_identity_token(scope: str, interactive: bool = True) -> Optional[str]:
    """A token for `scope` through azure-identity, or None when it cannot be had.

    The browser credential is appended only on a TTY: on a headless runner it would try to
    open a browser and block on a local redirect listener, which reads as a hang rather than
    a failure.
    """
    try:
        from azure.identity import AzureCliCredential
    except ImportError:
        return None
    chain = [AzureCliCredential]
    if interactive and sys.stdin is not None and sys.stdin.isatty():
        try:
            from azure.identity import InteractiveBrowserCredential
            chain.append(InteractiveBrowserCredential)
        except ImportError:
            pass
    debug = bool(os.environ.get("FABCONTEXT_AUTH_DEBUG"))
    for credential in chain:
        try:
            return credential().get_token(scope).token
        except Exception as exc:                    # noqa: BLE001 - try the next credential
            if debug:
                print("[auth] " + credential.__name__ + " failed for " + scope + ": "
                      + repr(exc), flush=True)
    return None


def _expiry(token: str) -> Optional[float]:
    """The `exp` of a JWT, or None when it is not a decodable one. No signature check - the
    only question is when to refresh."""
    if token in _EXPIRY:
        return _EXPIRY[token]
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)                # restore base64url padding
        exp = float(json.loads(base64.urlsafe_b64decode(seg.encode())).get("exp"))
    except Exception:                               # noqa: BLE001 - not a JWT
        exp = None
    with _LOCK:
        if len(_EXPIRY) >= 16:                      # a session holds a handful of tokens
            _EXPIRY.clear()
        _EXPIRY[token] = exp
    return exp


def is_expiring(token: Optional[str], margin_seconds: int = 600) -> bool:
    """Whether `token` is a JWT within `margin_seconds` of expiry. A token whose expiry
    cannot be read is left alone rather than churned."""
    if not token:
        return False
    exp = _expiry(token)
    return exp is not None and time.time() >= exp - margin_seconds


def _cached(scope: str, acquire: Callable[[], Optional[str]]) -> Optional[str]:
    key = (os.environ.get("AZURE_TENANT_ID") or "", scope)
    with _LOCK:
        held = _CACHE.get(key)
        if held and not is_expiring(held):
            return held
        token = acquire()
        if token:
            _CACHE[key] = token
        # A blip re-acquiring must not discard a token that is merely inside the margin: it
        # is still valid, and raising while holding a working token helps nobody.
        return token or held


def _token(scope: str, audience: str, hint: str) -> str:
    token = _cached(scope, lambda: (_notebook_token(audience)
                                    or os.environ.get(_ENV.get(scope, ""))
                                    or azure_identity_token(scope)))
    if token:
        return token
    raise RuntimeError(
        "could not acquire a token for " + hint + ". Inside a Fabric notebook this is "
        "automatic; elsewhere run `az login --scope " + scope + "`"
        + (", or set " + _ENV[scope] if scope in _ENV else ""))


def onelake_token() -> str:
    """OneLake storage. Goes into delta-rs `storage_options` and the DFS credential."""
    return _token(STORAGE_SCOPE, "storage", "OneLake storage")


def fabric_token() -> str:
    """The Fabric control plane. In a notebook the `pbi` audience covers it."""
    return _token(FABRIC_SCOPE, "pbi", "the Fabric API")


def powerbi_token() -> str:
    """The Power BI REST API - the scanner and the audit log."""
    return _token(POWERBI_SCOPE, "pbi", "the Power BI API")


def sql_token() -> str:
    """A SQL analytics endpoint, over TDS."""
    return _token(SQL_SCOPE, "pbi", "the SQL endpoint")


def kusto_token(cluster_uri: str) -> str:
    """An Eventhouse query endpoint. The audience is the cluster itself, so this one cannot
    be cached against a fixed scope like the others."""
    token = _notebook_token(cluster_uri)
    if token:
        return token
    scope = cluster_uri.rstrip("/") + "/.default"
    token = _cached(scope, lambda: azure_identity_token(scope))
    if token:
        return token
    raise RuntimeError("no token for " + cluster_uri + "; run `az login --scope " + scope + "`")
