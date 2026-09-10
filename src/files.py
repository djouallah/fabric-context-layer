"""The harvest's files - `raw/`, `build/`, `wiki/`, `graph.html` - kept in the lakehouse's
Files section rather than in the repo.

The repo holds Python and nothing else. The lakehouse holds both halves of the context: the
ranked graph as Delta tables under `Tables/`, and everything it was built from and rendered
into under `Files/`. Anyone with access to that one item can see the harvested JSON, the
parsed edges, the wiki and the graph page, without the machine that produced them.

Working copies live outside the repo too, one folder per context under `common.WORK`. A
push is a diff: files whose size or mtime changed since the last push go up, files that
disappeared locally come down, everything else is left alone. OneLake charges about 0.8 s
a file and a megabyte a second, so re-uploading 900 unchanged wiki pages would cost twelve
minutes and buy nothing.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Tuple

# What lives under Files/, and where it comes from locally. `code` is the harvest's own
# source, shipped by deploy.py so the scheduled notebook can stage it.
ITEMS = ("raw", "build", "wiki", "graph.html", "context.json")
MANIFEST = ".pushed.json"
SINGLE_SHOT_MAX = 128 * 1024 * 1024      # above this an overwrite streams instead of buffering


def _session(target: str):
    import logging

    import duckrun

    logging.getLogger("duckrun").setLevel(logging.WARNING)
    return duckrun.connect(target, read_only=True)   # read_only guards Delta, not Files


def _walk(root: str) -> List[Tuple[str, str]]:
    """[(local path, key relative to root)] for every file under root."""
    if os.path.isfile(root):
        return [(root, os.path.basename(root))]
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            local = os.path.join(dirpath, name)
            out.append((local, os.path.relpath(local, root).replace("\\", "/")))
    return out


def _stamp(path: str) -> List[int]:
    st = os.stat(path)
    return [st.st_size, int(st.st_mtime)]


def _manifest_path(work: str) -> str:
    return os.path.join(work, MANIFEST)


def _read_manifest(work: str) -> Dict[str, Dict[str, List[int]]]:
    try:
        with open(_manifest_path(work), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(work: str, data: Dict) -> None:
    with open(_manifest_path(work), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")


def push(target: str, work: str, items: Iterable[str] = ITEMS, full: bool = False,
         log=print) -> Dict[str, Dict[str, int]]:
    """Send the local working files to `Files/<item>` in the lakehouse, uploading only what
    changed since the last push and deleting what is gone. `full` ignores the manifest and
    sends everything. Returns {item: {"sent": n, "deleted": n, "skipped": n, "bytes": n}}."""
    from dbt.adapters.duckrun import objectstore, remote as dr_remote, secret

    session = _session(target)
    so = secret.refreshed(session.storage_options)
    manifest = {} if full else _read_manifest(work)
    out: Dict[str, Dict[str, int]] = {}
    for item in items:
        local_root = os.path.join(work, item)
        if not os.path.exists(local_root):
            continue
        remote_folder = item if os.path.isdir(local_root) else ""
        base = session._files_base(remote_folder)
        store = objectstore.build_store(base, so)
        abfss = dr_remote.is_abfss(base)
        known = dict(manifest.get(item) or {})
        now: Dict[str, List[int]] = {}
        sent = skipped = deleted = size = 0
        for local, key in _walk(local_root):
            stamp = _stamp(local)
            now[key] = stamp
            if known.get(key) == stamp:
                skipped += 1
                continue
            objectstore.upload(store, key, local,
                               single_shot=abfss and key in known
                               and stamp[0] < SINGLE_SHOT_MAX)
            sent += 1
            size += stamp[0]
            log("  [up] " + _label(item, key) + "  " + _human(stamp[0]))
        for key in known:
            if key not in now:
                objectstore.delete(store, key, base_url=base, storage_options=so)
                deleted += 1
                log("  [del] " + _label(item, key))
        manifest[item] = now
        out[item] = {"sent": sent, "deleted": deleted, "skipped": skipped, "bytes": size}
    _write_manifest(work, manifest)
    return out


def pull(target: str, work: str, items: Iterable[str] = ITEMS, log=print) -> Dict[str, int]:
    """Bring the files back down from the lakehouse into the working folder. The mirror of
    push, for a second machine or after the working folder is cleared."""
    session = _session(target)
    out: Dict[str, int] = {}
    os.makedirs(work, exist_ok=True)
    for item in items:
        local_root = os.path.join(work, item)
        before = len(_walk(local_root)) if os.path.exists(local_root) else 0
        if os.path.splitext(item)[1]:
            # A single file sits at the root of Files/; the extension filter keeps the
            # download to it rather than the whole section.
            session.download("", work, file_extensions=[os.path.splitext(item)[1]],
                             overwrite=True)
        else:
            session.download(item, local_root, overwrite=True)
        after = len(_walk(local_root)) if os.path.exists(local_root) else 0
        out[item] = after
        log("  " + item + ": " + str(after) + " files (" + str(after - before) + " new)")
    # The files now match the remote by construction; record that so the next push is quiet.
    manifest = {}
    for item in items:
        local_root = os.path.join(work, item)
        if os.path.exists(local_root):
            manifest[item] = {key: _stamp(local) for local, key in _walk(local_root)}
    _write_manifest(work, manifest)
    return out


def listing(target: str, items: Iterable[str] = ITEMS) -> Dict[str, List[str]]:
    """What is under Files/ in the lakehouse right now."""
    from dbt.adapters.duckrun import objectstore, secret

    session = _session(target)
    so = secret.refreshed(session.storage_options)
    out: Dict[str, List[str]] = {}
    for item in items:
        if item.endswith(".html"):
            continue
        base = session._files_base(item)
        try:
            out[item] = sorted(objectstore.list_keys(objectstore.build_store(base, so)))
        except Exception as exc:                   # noqa: BLE001 - the folder may not exist yet
            out[item] = ["[" + exc.__class__.__name__ + "] " + str(exc)[:80]]
    return out


def _label(item: str, key: str) -> str:
    """Files/<what the portal shows>."""
    return "Files/" + (item + "/" + key if item != key else key)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (("%.0f" if unit == "B" else "%.1f") % n) + " " + unit
        n /= 1024.0
    return str(n)
