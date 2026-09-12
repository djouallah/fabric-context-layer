"""OneLake: the URLs, the table listing, and moving loose files in and out of `Files/`.

OneLake speaks the ADLS Gen2 API, so the file transfer is the stock Azure SDK pointed at
`onelake.dfs.fabric.microsoft.com` with the workspace as the filesystem. There is nothing to
hand-roll and no object-store library to add.

Addresses are always built from two GUIDs the caller already holds, never parsed back out of
a user-supplied string. That removes a whole class of confusion about whether `a/b` means a
workspace and a lakehouse or a relative directory.

`LocalStore` is the same four operations over a directory. It is not a test double: the
offline suite runs the real push, the real diff and the real Delta write against a folder.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional

ONELAKE_HOST = "onelake.dfs.fabric.microsoft.com"
ACCOUNT_URL = "https://" + ONELAKE_HOST


def tables_root(workspace_id: str, item_id: str) -> str:
    """The abfss root delta-rs writes tables under."""
    return "abfss://" + workspace_id + "@" + ONELAKE_HOST + "/" + item_id + "/Tables"


def files_root(workspace_id: str, item_id: str) -> str:
    return "abfss://" + workspace_id + "@" + ONELAKE_HOST + "/" + item_id + "/Files"


def portal_url(workspace_id: str, item_id: str) -> str:
    return ("https://app.fabric.microsoft.com/groups/" + workspace_id
            + "/lakehouses/" + item_id)


def storage_options(token: str) -> Dict[str, str]:
    """What delta-rs needs to read and write OneLake."""
    return {"bearer_token": token}


class _StaticCredential:
    """A bearer token dressed as a `TokenCredential` for the Azure SDK."""

    def __init__(self, token: str):
        self._token = token

    def get_token(self, *scopes, **kwargs):         # noqa: D102 - SDK protocol
        from azure.core.credentials import AccessToken

        from . import auth

        token = self._token
        if auth.is_expiring(token):
            token = auth.onelake_token()
            self._token = token
        expiry = auth._expiry(token)                # noqa: SLF001 - same package
        return AccessToken(token, int(expiry) if expiry else 0)


# ---------------------------------------------------------------- stores

class OneLakeStore:
    """One section (`Files` by default) of one Fabric item, as a flat key/value store."""

    def __init__(self, workspace_id: str, item_id: str, section: str = "Files",
                 token: Optional[str] = None):
        from azure.storage.filedatalake import DataLakeServiceClient

        from . import auth

        self.workspace_id = workspace_id
        self.item_id = item_id
        self.section = section
        self.token = token or auth.onelake_token()
        self._fs = DataLakeServiceClient(
            ACCOUNT_URL, credential=_StaticCredential(self.token)
        ).get_file_system_client(workspace_id)

    @property
    def tables_root(self) -> str:
        return tables_root(self.workspace_id, self.item_id)

    @property
    def storage_options(self) -> Optional[Dict[str, str]]:
        return storage_options(self.token)

    def _path(self, key: str) -> str:
        base = self.item_id + "/" + self.section
        return base + "/" + key.strip("/") if key else base

    def list(self, prefix: str = "") -> List[str]:
        """Every file key under `prefix`, relative to the section. An absent directory lists
        as empty - it simply has not been written yet."""
        from azure.core.exceptions import ResourceNotFoundError

        base = self._path(prefix)
        try:
            paths = self._fs.get_paths(path=base, recursive=True)
            return sorted(p.name[len(base):].lstrip("/") for p in paths if not p.is_directory)
        except ResourceNotFoundError:
            return []

    def upload(self, key: str, local_path: str) -> int:
        """Send one file, replacing what is there. Streamed from the handle, so size is not a
        consideration."""
        with open(local_path, "rb") as handle:
            self._fs.get_file_client(self._path(key)).upload_data(handle, overwrite=True)
        return os.path.getsize(local_path)

    def download(self, key: str, local_path: str) -> None:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        stream = self._fs.get_file_client(self._path(key)).download_file()
        with open(local_path, "wb") as handle:
            stream.readinto(handle)

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._fs.get_file_client(self._path(key)).delete_file()
        except ResourceNotFoundError:
            pass

    def list_table_dirs(self) -> List[Dict[str, str]]:
        """`[{"schema", "name"}]` for the Delta tables under `Tables/`.

        A schema-enabled lakehouse has one level of schema directories above the tables; an
        older one has the tables directly. Which it is shows in whether a directory contains
        `_delta_log`, so two listings settle it without walking the whole store.
        """
        from azure.core.exceptions import ResourceNotFoundError

        base = self.item_id + "/Tables"
        out: List[Dict[str, str]] = []
        try:
            top = [p for p in self._fs.get_paths(path=base, recursive=False) if p.is_directory]
        except ResourceNotFoundError:
            return out
        for entry in top:
            name = entry.name.rsplit("/", 1)[-1]
            try:
                children = list(self._fs.get_paths(path=entry.name, recursive=False))
            except ResourceNotFoundError:
                children = []
            child_names = {c.name.rsplit("/", 1)[-1] for c in children}
            if "_delta_log" in child_names:
                out.append({"schema": "dbo", "name": name})
                continue
            for child in children:
                if child.is_directory:
                    out.append({"schema": name, "name": child.name.rsplit("/", 1)[-1]})
        return out


class LocalStore:
    """The same four operations over a directory, for a run with no tenant."""

    def __init__(self, root: str, section: str = "Files"):
        self.base = root
        self.root = os.path.join(root, section) if section else root
        self.section = section

    @property
    def tables_root(self) -> str:
        """A local store mirrors a lakehouse - `Tables/` beside `Files/` - so the layout the
        test exercises is the layout production writes."""
        return os.path.join(self.base, "Tables")

    @property
    def storage_options(self) -> Optional[Dict[str, str]]:
        return None

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.strip("/").split("/"))

    def list(self, prefix: str = "") -> List[str]:
        base = os.path.join(self.root, *prefix.strip("/").split("/")) if prefix else self.root
        if not os.path.isdir(base):
            return []
        out = []
        for folder, _dirs, names in os.walk(base):
            for name in names:
                full = os.path.join(folder, name)
                out.append(os.path.relpath(full, base).replace("\\", "/"))
        return sorted(out)

    def upload(self, key: str, local_path: str) -> int:
        dest = self._path(key)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(local_path, dest)
        return os.path.getsize(dest)

    def download(self, key: str, local_path: str) -> None:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        shutil.copyfile(self._path(key), local_path)

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except OSError:
            pass

    def list_table_dirs(self) -> List[Dict[str, str]]:
        base = self.tables_root
        out: List[Dict[str, str]] = []
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            entry = os.path.join(base, name)
            if not os.path.isdir(entry):
                continue
            children = sorted(os.listdir(entry))
            if "_delta_log" in children:
                out.append({"schema": "dbo", "name": name})
                continue
            for child in children:
                if os.path.isdir(os.path.join(entry, child)):
                    out.append({"schema": name, "name": child})
        return out
