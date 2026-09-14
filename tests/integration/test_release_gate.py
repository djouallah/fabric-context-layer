"""The release gate: one harvest of a real tenant, asserted end to end.

Everything else in `tests/` runs on a synthetic tenant with no network, which is what makes the
suite runnable on a laptop - and is also how a 400 on the very first real lakehouse create
reached PyPI. An offline suite can only ever prove the half of this package that does not talk
to Fabric.

So this one is marked `tenant`, deselected from `pytest` by the `addopts` in `pyproject.toml`,
and run only by `.github/workflows/release-gate.yml` before a tag is published. It needs a
token: workload-identity federation on a runner, `az login` on a laptop.

It publishes to the real `context_layer`, refreshing it rather than making a throwaway. So the
create path it was written for only fires the first time; from then on this covers the update
path, the placement, the model and the read-back - which is the part that can rot without
anyone noticing.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.tenant

SOURCE = os.environ.get("FABCONTEXT_GATE_WORKSPACE", "aemodev")
FOLDER = "context"


@pytest.fixture(scope="module")
def published():
    """One harvest, shared by every assertion below - it is minutes of tenant work, not seconds."""
    import fabcontext

    url = fabcontext.harvest(SOURCE, folder=FOLDER)
    assert url.startswith("abfss://"), url
    return url


def test_the_harvest_lands_where_the_fixed_address_says(published):
    """The whole point of the default target: a tenant-wide constant an agent can find."""
    from fabcontext import DEFAULT_LAKEHOUSE, DEFAULT_WORKSPACE
    from fabcontext._fabric import Workspace
    from fabcontext.semantic_model import ids_from_url

    workspace_id, item_id = ids_from_url(published)
    ws = Workspace(DEFAULT_WORKSPACE)
    assert ws.id == workspace_id, "published outside the workspace the default names"
    lakehouses = {lh["displayName"]: lh["id"] for lh in ws.list_lakehouses()}
    assert lakehouses.get(DEFAULT_LAKEHOUSE) == item_id


def test_the_lakehouse_is_actually_in_the_folder(published):
    """`folder=` used to be able to fail a publish, and then could not place a new lakehouse at
    all - the create body rejects `folderId`, so placement is a move afterwards. Surviving the
    publish is not the assertion; being in the folder is."""
    from fabcontext._fabric import Workspace
    from fabcontext._fabric.rest import FABRIC_API, request
    from fabcontext.semantic_model import ids_from_url

    workspace_id, item_id = ids_from_url(published)
    ws = Workspace(workspace_id)
    folder_id = ws.ensure_folder(FOLDER)
    assert folder_id, "the folders API is reachable, so the folder should exist"

    resp = request("GET", FABRIC_API + "/workspaces/" + workspace_id + "/items/" + item_id,
                   token=ws.token)
    resp.raise_for_status()
    assert resp.json().get("folderId") == folder_id


def test_the_context_model_was_created(published):
    """The model step swallows its own failure so a tenant that refuses it still gets a context
    (`fabcontext/__init__.py`). Nothing else would ever notice it had stopped working - and the
    asking side finds the context by this name."""
    from fabcontext._fabric import Workspace
    from fabcontext.semantic_model import DEFAULT_NAME, ids_from_url

    workspace_id, _item_id = ids_from_url(published)
    names = {model.get("displayName") for model in Workspace(workspace_id).list_semantic_models()}
    assert DEFAULT_NAME in names, sorted(names)


def test_the_published_tables_read_back(published):
    """The data plane, not just the control plane: a lakehouse in the right folder holding an
    empty graph would pass everything above."""
    import fabcontext

    con = fabcontext.open_context(published)
    try:
        assert con.execute("SELECT count(*) FROM definitions").fetchone()[0] > 0
        assert con.execute("SELECT count(*) FROM terms").fetchone()[0] > 0
    finally:
        con.close()
