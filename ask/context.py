"""The client half: find the lakehouse, fetch `Files/context.md`.

The harvest publishes the ranked graph twice - as Delta tables under `Tables/`, and as one
markdown file under `Files/`. This reads the file. That is the whole of it: no DuckDB, no
Delta reader, no local copy of a database, no query language. An agent reads the markdown
and, when a question needs a number, runs DAX against the model whose ids the markdown
carries (`ask/fabric.py`).

The tables are still the contract - the file is rendered from them by the same run that
published them - but reading them is the benchmark's job now, not the client's
(`ask/db.py`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from typing import Any, Dict, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATION = os.path.join(ROOT, "context.json")
CONTEXT_MD = "context.md"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ABFSS = re.compile(r"^abfss://([^@/]+)@[^/]+/([^/]+)(?:/|$)", re.I)
# Where the fetched copy is kept - outside the repo, which holds no context.
CACHE_DIR = os.environ.get("FABRIC_CONTEXT_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "fabric-context")


class NotFound(Exception):
    pass


def location() -> Optional[Dict[str, Any]]:
    """Where the harvest published the context. An address, never content."""
    try:
        with open(LOCATION, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("path") else None


def default_db() -> str:
    where = location()
    if not where:
        raise NotFound("nothing published yet - pass --db with the URL fabcontext.harvest() "
                       "returned")
    return where["path"]


def is_remote(path: str) -> bool:
    """Whether `path` names a lakehouse rather than a local folder."""
    low = path.lower()
    if low.startswith(("abfss://", "az://", "s3://", "gs://")):
        return True
    parts = path.split("/")
    return len(parts) >= 2 and (parts[1].lower().endswith(".lakehouse")
                                or bool(_GUID.match(parts[0]) and _GUID.match(parts[1])))


def lakehouse_ids(path: str) -> Tuple[str, str]:
    """(workspace_id, item_id) out of an address.

    Also how `dax` reads its target: `context.md` prints both ids on every model, so
    `<workspace-guid>/<model-guid>` is copied from the file and needs no lookup."""
    match = _ABFSS.match(path)
    if match:
        return match.group(1), match.group(2)
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) >= 2 and _GUID.match(parts[0]) and _GUID.match(parts[1]):
        return parts[0], parts[1]
    raise NotFound("cannot tell which workspace and item " + repr(path) + " names - it is "
                   "<workspace-guid>/<item-guid>, or the abfss:// URL the harvest returned")


def context_file(path: str, refresh: bool = False) -> Dict[str, Any]:
    """`Files/context.md` - the whole context as one markdown file - in one round trip.

    One request against OneLake, against the twelve Delta logs the old query side opened.
    The file is kept under CACHE_DIR and re-fetched on `--refresh`; a new publish overwrites
    it in place, which is why the harvest stamps `built_at` into its header.
    """
    if path.lower().endswith(".duckdb"):
        raise NotFound("a .duckdb copy holds the tables only - point --db at the lakehouse "
                       "the harvest returned to read " + CONTEXT_MD)
    if not is_remote(path):
        base = os.path.abspath(path.rstrip("/\\"))
        for candidate in (os.path.join(os.path.dirname(base), "Files", CONTEXT_MD),
                          os.path.join(base, "Files", CONTEXT_MD),
                          os.path.join(os.path.dirname(base), CONTEXT_MD)):
            if os.path.isfile(candidate):
                return _result(path, candidate)
        raise NotFound("no " + CONTEXT_MD + " beside " + path
                       + " - the harvest writes it into the lakehouse's Files section")
    workspace_id, item_id = lakehouse_ids(path)
    local = os.path.join(CACHE_DIR, item_id + "-" + CONTEXT_MD)
    if refresh or not os.path.exists(local):
        # The one import that reaches outside: OneLake file access, shared with the harvest
        # so there is a single implementation of a token and a path.
        from fabcontext._fabric import onelake

        os.makedirs(CACHE_DIR, exist_ok=True)
        onelake.OneLakeStore(workspace_id, item_id).download(CONTEXT_MD, local)
    return _result(path, local)


def _result(db: str, local: str) -> Dict[str, Any]:
    stat = os.stat(local)
    return {"db": db, "path": local, "bytes": stat.st_size,
            "fetched_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "text": None}
