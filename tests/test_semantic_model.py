"""The context's own semantic model, built without a tenant.

The create call needs Fabric; the TMSL does not, and the TMSL is where the mistakes live. The
sharpest test here is the round trip: this repo already parses Direct Lake models to find the
lakehouse behind them, so a model it generates must be one it can read back.
"""
from __future__ import annotations

import base64
import json

from fabcontext import semantic_model as sm
from fabcontext._fabric import patterns

WS = "11111111-2222-3333-4444-555555555555"
LH = "66666666-7777-8888-9999-000000000000"


def _bim(con):
    schema = {table: sm.columns(con, table) for table in sm.TABLES}
    return sm.bim(WS, LH, schema)


def _table(model, name):
    return next(t for t in model["model"]["tables"] if t["name"] == name)


def test_data_types_map_and_fall_back_to_string():
    assert sm.data_type("BIGINT") == "int64"
    assert sm.data_type("DOUBLE") == "double"
    assert sm.data_type("BOOLEAN") == "boolean"
    assert sm.data_type("TIMESTAMP WITH TIME ZONE") == "dateTime"
    assert sm.data_type("VARCHAR") == "string"
    # Unknown types are readable rather than rejected - a new DuckDB type must not break a run.
    assert sm.data_type("STRUCT(a INTEGER)") == "string"


def test_the_one_table_is_direct_lake_over_the_lakehouse(con):
    """One denormalised table, no relationship: a client needs one equality filter and
    nothing to join, and there is no second path for a query to take by accident."""
    model = _bim(con)
    names = [t["name"] for t in model["model"]["tables"]]
    assert names == list(sm.TABLES) == ["answers"]
    assert model["model"]["relationships"] == []
    for table in model["model"]["tables"]:
        part, = table["partitions"]
        assert part["mode"] == "directLake"
        assert part["source"] == {"type": "entity", "entityName": table["name"],
                                  "schemaName": "dbo", "expressionSource": sm.EXPRESSION}
        assert "measures" not in table


def test_every_partition_points_at_an_expression_that_exists(con):
    """A partition naming an expression the model does not define loads no table at all, and
    Fabric accepts the definition anyway - so the mismatch only shows up as a failed query."""
    model = _bim(con)
    defined = {e["name"] for e in model["model"]["expressions"]}
    referenced = {t["partitions"][0]["source"]["expressionSource"]
                  for t in model["model"]["tables"]}
    assert referenced <= defined, (referenced - defined)


def test_our_own_parser_reads_the_model_we_emit(con):
    """The harvest identifies a Direct Lake model by the OneLake URL in its M expression and
    by its entity partitions. Emitting a model it cannot read would be a silent split."""
    model = _bim(con)
    expression = "\n".join(model["model"]["expressions"][0]["expression"])
    found = patterns.ONELAKE_REF.search(expression)
    assert found and found.group(1) == WS and found.group(2) == LH
    for table in model["model"]["tables"]:
        assert patterns.partition_table(table["partitions"][0], table["name"]) == (
            "dbo", table["name"])


def test_the_model_is_curated_and_every_column_says_what_it_means(con):
    """The field list is what a metadata-driven agent reads. Every column the model exposes
    is named in COLUMNS with a description and exists in the built table; a published column
    not named there is deliberately not in the model."""
    model = _bim(con)
    for table in model["model"]["tables"]:
        built = {name for name, _t in sm.columns(con, table["name"])}
        assert table.get("description"), table["name"]
        names = [c["name"] for c in table["columns"]]
        assert names == [n for n, _d in sm.COLUMNS[table["name"]]], table["name"]
        for column in table["columns"]:
            assert column["name"] in built, (table["name"], column["name"])
            assert column.get("description"), (table["name"], column["name"])


def test_the_ids_an_agent_needs_are_columns_of_the_table(con):
    columns = {c["name"] for c in _table(_bim(con), "answers")["columns"]}
    for needed in ("term", "alias_norm", "rank", "measure", "description", "model_id",
                   "workspace_id", "dax", "confidence", "rivals"):
        assert needed in columns, needed
    assert "def_id" not in columns


def test_lineage_tags_are_stable_across_builds(con):
    """A second harvest updates this model. Random tags would churn every column's identity."""
    assert json.dumps(_bim(con), sort_keys=True) == json.dumps(_bim(con), sort_keys=True)


def test_definition_parts_carry_both_files_as_base64(con):
    parts = {p["path"]: p for p in sm.definition_parts(_bim(con))}
    assert set(parts) == {"model.bim", "definition.pbism"}
    for part in parts.values():
        assert part["payloadType"] == "InlineBase64"
        json.loads(base64.b64decode(part["payload"]).decode("utf-8"))


def test_the_ids_come_out_of_the_address_harvest_returned():
    """The model can be built over a lakehouse published long ago, with no state on the
    machine - so both ids must be recoverable from the URL alone."""
    import pytest

    url = "abfss://" + WS + "@onelake.dfs.fabric.microsoft.com/" + LH + "/Tables"
    assert sm.ids_from_url(url) == (WS, LH)
    assert sm.ids_from_url(WS + "/" + LH) == (WS, LH)
    with pytest.raises(ValueError):
        sm.ids_from_url("C:/somewhere/copy.duckdb")


def test_a_local_store_creates_nothing(con):
    """The offline suite must stay offline: no workspace, no call, no failure."""
    class _Local:
        pass

    assert sm.ensure(con, _Local()) is None
