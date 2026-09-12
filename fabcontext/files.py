"""The harvest's working files - `raw/`, `build/`, `wiki/`, `graph.html` - kept in the
lakehouse's Files section rather than on whatever machine ran it.

`raw/` is the load-bearing one. It is the previous harvest, and pulling it back down is what
makes the next run incremental: a definition refetches only when its `lastUpdatedDate` has
moved, and the audit log is one file per UTC day. Without it, every run is a full harvest.

A push is a diff. Files whose size or mtime changed go up, files that disappeared locally are
deleted remotely, and everything else is left alone - each file is its own round trip, so
re-sending nine hundred unchanged wiki pages would cost minutes and buy nothing.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Tuple

# What lives under Files/, and where it comes from in the working folder.
ITEMS = ("raw", "build", "wiki", "graph.html")
MANIFEST = ".pushed.json"


def _walk(root: str) -> List[Tuple[str, str]]:
    """[(local path, key relative to root)] for every file under root."""
    if os.path.isfile(root):
        return [(root, os.path.basename(root))]
    out = []
    for folder, _dirs, names in os.walk(root):
        for name in names:
            local = os.path.join(folder, name)
            out.append((local, os.path.relpath(local, root).replace("\\", "/")))
    return out


def _stamp(path: str) -> List[int]:
    st = os.stat(path)
    return [st.st_size, int(st.st_mtime)]


def _read_manifest(work: str) -> Dict[str, Dict[str, List[int]]]:
    try:
        with open(os.path.join(work, MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(work: str, data: Dict) -> None:
    with open(os.path.join(work, MANIFEST), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")


def _key(item: str, key: str) -> str:
    """Where one file sits under Files/. A folder item keeps its tree; a single file sits at
    the root of the section, which is how the portal shows it."""
    return key if item == key else item + "/" + key


def push(store, work: str, items: Iterable[str] = ITEMS, full: bool = False,
         log=print) -> Dict[str, Dict[str, int]]:
    """Send the working files up, uploading only what changed since the last push and
    deleting what is gone. `full` ignores the manifest and sends everything.

    Returns {item: {"sent", "deleted", "skipped", "bytes"}}.
    """
    manifest = {} if full else _read_manifest(work)
    out: Dict[str, Dict[str, int]] = {}
    for item in items:
        local_root = os.path.join(work, item)
        if not os.path.exists(local_root):
            continue
        known = dict(manifest.get(item) or {})
        now: Dict[str, List[int]] = {}
        sent = skipped = deleted = size = 0
        for local, key in _walk(local_root):
            stamp = _stamp(local)
            now[key] = stamp
            if known.get(key) == stamp:
                skipped += 1
                continue
            size += store.upload(_key(item, key), local)
            sent += 1
            log("  [up] Files/" + _key(item, key) + "  " + human(stamp[0]))
        for key in known:
            if key not in now:
                store.delete(_key(item, key))
                deleted += 1
                log("  [del] Files/" + _key(item, key))
        manifest[item] = now
        out[item] = {"sent": sent, "deleted": deleted, "skipped": skipped, "bytes": size}
    _write_manifest(work, manifest)
    return out


def pull(store, work: str, items: Iterable[str] = ITEMS, log=print) -> Dict[str, int]:
    """Bring the files back down into the working folder - the mirror of push, and the first
    thing a run does against a lakehouse that already exists."""
    out: Dict[str, int] = {}
    os.makedirs(work, exist_ok=True)
    manifest = _read_manifest(work)
    for item in items:
        single = bool(os.path.splitext(item)[1])
        keys = [item] if single else store.list(item)
        local_root = os.path.join(work, item)
        for key in keys:
            local = local_root if single else os.path.join(local_root, *key.split("/"))
            try:
                store.download(_key(item, key) if not single else item, local)
            except Exception as exc:              # noqa: BLE001 - a missing file is not fatal
                log("  [warn] " + _key(item, key) + ": " + str(exc)[:120])
        out[item] = len(keys)
        log("  " + item + ": " + str(len(keys)) + " files")
        # What just landed matches the remote by construction; record it so the push at the
        # end of the run sends only what this run actually changed.
        if os.path.exists(local_root):
            manifest[item] = {key: _stamp(local) for local, key in _walk(local_root)}
    _write_manifest(work, manifest)
    return out


def listing(store, items: Iterable[str] = ITEMS) -> Dict[str, List[str]]:
    """What is under Files/ right now."""
    return {item: store.list(item) for item in items if not os.path.splitext(item)[1]}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (("%.0f" if unit == "B" else "%.1f") % n) + " " + unit
        n /= 1024.0
    return str(n)
