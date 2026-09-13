"""A control-plane handle for one workspace: resolve it, list what is in it, and make the
lakehouse the context is published into.

Deliberately small. The harvest reads a tenant and writes one item; it never deploys a
notebook or drives a schedule, so none of that is here.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import auth
from .rest import (FABRIC_API, POLL_INTERVAL, POLL_TIMEOUT, POWERBI_API, FabricError,
                   _sleep, await_lro, await_lro_item_id,
                   looks_like_guid, paged, request)


class Workspace:
    """One workspace, addressed by display name or GUID."""

    def __init__(self, workspace: str, token: Optional[str] = None):
        self.name = workspace
        self.token = token or auth.fabric_token()
        self.id = self._resolve(workspace)
        self._display_name: Optional[str] = None

    def _resolve(self, workspace: str) -> str:
        """The workspace GUID. A value that already is one is taken at face value rather than
        listing every workspace the token can see."""
        if looks_like_guid(workspace):
            return workspace
        for ws in paged(FABRIC_API + "/workspaces", self.token):
            if ws.get("displayName") == workspace:
                return ws["id"]
        raise FabricError("workspace " + repr(workspace)
                          + " not found, or the token cannot see it")

    @property
    def display_name(self) -> str:
        """The workspace's own name. Cached, because the harvest labels everything it writes
        with the name a person would recognise rather than the GUID."""
        if self._display_name is None:
            self._display_name = self.info().get("displayName") or self.id
        return self._display_name

    def info(self) -> Dict:
        resp = request("GET", FABRIC_API + "/workspaces/" + self.id, token=self.token)
        resp.raise_for_status()
        return resp.json()

    def list_items(self, kind: Optional[str] = None) -> List[Dict]:
        """Every item in the workspace, each tagged with its type. `kind` narrows to one REST
        collection - lakehouses, semanticModels and so on - whose rows carry no type, because
        the collection is the type."""
        return paged(FABRIC_API + "/workspaces/" + self.id + "/" + (kind or "items"), self.token)

    def list_lakehouses(self) -> List[Dict]:
        return self.list_items("lakehouses")

    def create_lakehouse(self, name: str, schemas: bool = True,
                         folder: Optional[str] = None) -> str:
        """Ensure a lakehouse called `name` exists here; return its id.

        Idempotent, which is what makes a harvest create-or-update: an existing lakehouse is
        returned untouched and left where it already lives, so `folder` only places one this
        call actually creates.
        """
        for lakehouse in self.list_lakehouses():
            if lakehouse.get("displayName") == name:
                return lakehouse["id"]
        body: Dict = {"displayName": name}
        if schemas:
            body["creationPayload"] = {"enableSchemas": True}
        if folder:
            body["folderId"] = self.ensure_folder(folder, required=True)
        resp = request("POST", FABRIC_API + "/workspaces/" + self.id + "/lakehouses",
                       token=self.token, json_body=body)
        if resp.status_code in (200, 201):
            return resp.json()["id"]
        if resp.status_code == 202:
            return await_lro_item_id(self.token, resp)
        resp.raise_for_status()
        raise FabricError("unexpected status " + str(resp.status_code)
                          + " creating the lakehouse: " + resp.text[:200])

    def list_semantic_models(self) -> List[Dict]:
        return self.list_items("semanticModels")

    def create_semantic_model(self, name: str, parts: List[Dict[str, str]],
                              folder: Optional[str] = None) -> str:
        """Ensure a semantic model called `name` exists here with `parts` as its definition.

        Idempotent the same way `create_lakehouse` is, and for the same reason: a harvest runs
        again and again, so an existing model has its definition replaced rather than a second
        model appearing beside it. Replacing the definition is also what picks up a new column
        - the tables are Direct Lake, so the data needs no reload, but the schema does.
        """
        body: Dict = {"definition": {"parts": parts}}
        for model in self.list_semantic_models():
            if model.get("displayName") == name:
                item_id = model["id"]
                resp = request("POST", FABRIC_API + "/workspaces/" + self.id
                               + "/semanticModels/" + item_id + "/updateDefinition",
                               token=self.token, json_body=body)
                if resp.status_code == 202:
                    await_lro(self.token, resp)
                elif resp.status_code not in (200, 201):
                    raise FabricError("could not update semantic model " + repr(name)
                                      + " (HTTP " + str(resp.status_code) + "): "
                                      + resp.text[:200])
                return item_id
        body["displayName"] = name
        if folder:
            body["folderId"] = self.ensure_folder(folder, required=True)
        resp = request("POST", FABRIC_API + "/workspaces/" + self.id + "/semanticModels",
                       token=self.token, json_body=body)
        if resp.status_code in (200, 201):
            return resp.json()["id"]
        if resp.status_code == 202:
            return await_lro_item_id(self.token, resp)
        raise FabricError("could not create semantic model " + repr(name) + " (HTTP "
                          + str(resp.status_code) + "): " + resp.text[:200])

    def refresh_semantic_model(self, item_id: str, retries: int = 3) -> str:
        """Reframe a Direct Lake model onto the latest Delta data, and wait for it.

        This is not optional. A model that has never been framed answers every query with
        "cannot find table" - the definition is right, the tables simply are not loaded yet -
        so a deploy that skips it looks like a broken model rather than an unfinished one.

        Retried, because right after a create the model's OneLake read permission can still be
        propagating and the first reframe fails transiently.

        Uses the Power BI API, so it needs a Power BI-scoped token rather than the Fabric
        control-plane one this class otherwise holds.
        """
        from . import auth

        base = (POWERBI_API + "/groups/" + self.id + "/datasets/" + item_id + "/refreshes")
        done = ("Completed", "Failed", "Disabled", "Cancelled")
        status = "Unknown"
        for attempt in range(max(1, retries)):
            token = auth.powerbi_token()
            resp = request("POST", base, token=token, json_body={})
            if resp.status_code not in (200, 202):
                raise FabricError("could not start the reframe (HTTP "
                                  + str(resp.status_code) + "): " + resp.text[:200])
            for _ in range(int(POLL_TIMEOUT // max(POLL_INTERVAL, 1)) + 1):
                _sleep(POLL_INTERVAL)
                poll = request("GET", base, token=token, params={"$top": 1})
                poll.raise_for_status()
                entries = poll.json().get("value") or []
                status = (entries[0].get("status") if entries else "Unknown")
                if status in done:
                    break
            if status == "Completed":
                return status
            if attempt == retries - 1:
                raise FabricError("the model was created but never framed (last reframe "
                                  + str(status) + "); it will answer 'cannot find table' "
                                  "until a refresh succeeds")
        return status

    # ---------------------------------------------------------- cosmetic placement

    def ensure_folder(self, name: str, required: bool = False) -> Optional[str]:
        """The id of the root-level workspace folder `name`, created if absent.

        Best effort unless `required`: a tenant without the folders API should still get its
        context published, just at the workspace root.
        """
        try:
            for folder in paged(FABRIC_API + "/workspaces/" + self.id + "/folders", self.token):
                if folder.get("displayName") == name and not folder.get("parentFolderId"):
                    return folder["id"]
            resp = request("POST", FABRIC_API + "/workspaces/" + self.id + "/folders",
                           token=self.token, json_body={"displayName": name})
            if resp.status_code in (200, 201):
                return resp.json()["id"]
            if required:
                raise FabricError("could not create workspace folder " + repr(name) + " (HTTP "
                                  + str(resp.status_code) + "): " + resp.text[:200])
        except FabricError:
            raise
        except Exception as exc:                    # noqa: BLE001 - cosmetic unless required
            if required:
                raise FabricError("could not resolve workspace folder " + repr(name)
                                  + ": " + str(exc)) from exc
        return None

    def move_item(self, item_id: str, folder: str) -> Optional[str]:
        """Move an existing item into a workspace folder. Idempotent and best effort: an item
        that cannot be moved is still a working item, so a tenant without the folders API must
        not fail a publish over where the icon sits."""
        try:
            folder_id = self.ensure_folder(folder, required=True)
            if not folder_id:
                return None
            base = FABRIC_API + "/workspaces/" + self.id + "/items/" + item_id
            resp = request("POST", base + "/move", token=self.token,
                           json_body={"targetFolderId": folder_id})
            if resp.status_code not in (200, 201, 202):
                resp = request("PATCH", base, token=self.token,
                               json_body={"folderId": folder_id})
            return folder_id if resp.status_code in (200, 201, 202) else None
        except Exception:                           # noqa: BLE001 - placement is cosmetic
            return None
