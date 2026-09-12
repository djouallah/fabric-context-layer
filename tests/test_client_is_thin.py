"""The query side is one markdown file and one REST call.

`ask` used to pull twelve Delta tables into DuckDB before it could say anything, and could
compute a number in SQL over a table no measure defined. Both are gone: the client reads
`Files/context.md` and runs DAX. These tests pin that, because it is the kind of property
a single convenient import quietly undoes.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_client_loads_no_database_driver():
    """duckdb and deltalake are the harvest's, not the client's. Importing them here would
    put two native wheels back in front of anyone who only wants to read the file."""
    code = ("import sys, ask.context, ask.fabric, ask.__main__;"
            "print(','.join(m for m in ('duckdb', 'deltalake', 'pyarrow')"
            " if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "", "the client imported " + out.stdout.strip()


def test_no_sql_path_survives():
    """A number with no agreed definition behind it is what the layer exists to prevent."""
    from ask import context as ctx
    from ask import fabric

    for name in ("raw_sql", "stores", "attach", "search", "define", "model", "table",
                 "lineage", "usage", "scope", "contract", "open_db"):
        assert not hasattr(ctx, name), name + " is still on the client"
    for name in ("sql_endpoint", "sql_token", "attach_store", "values"):
        assert not hasattr(fabric, name), name + " is still on the client"


def test_dax_takes_its_ids_from_the_file(monkeypatch):
    """`context.md` prints `<workspace_id>/<item_id>`; `dax` takes exactly that and looks
    nothing up, so running a number opens no database."""
    from ask import __main__ as cli
    from ask import fabric

    seen = {}

    def fake_dax(workspace_id, dataset_id, query, max_rows=100):
        seen.update(workspace_id=workspace_id, dataset_id=dataset_id, query=query)
        return {"query": query, "columns": ["[v]"], "rows": [{"[v]": 1}],
                "row_count": 1, "truncated": False, "elapsed_ms": 1}

    monkeypatch.setattr(fabric, "dax", fake_dax)
    ws, model = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333"
    assert cli.main(["dax", ws + "/" + model, 'EVALUATE ROW("v", [Total Revenue])']) == 0
    assert seen["workspace_id"] == ws and seen["dataset_id"] == model


def test_dax_says_so_when_the_ids_are_not_ids():
    from ask import __main__ as cli

    assert cli.main(["dax", "Sales Model", "EVALUATE ROW(\"v\", [X])"]) == 2
