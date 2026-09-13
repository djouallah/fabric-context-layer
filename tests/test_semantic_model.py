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


def test_the_three_tables_are_direct_lake_over_the_lakehouse(con):
    model = _bim(con)
    names = [t["name"] for t in model["model"]["tables"]]
    assert names == list(sm.TABLES)
    for table in model["model"]["tables"]:
        part, = table["partitions"]
        assert part["mode"] == "directLake"
        assert part["source"] == {"type": "entity", "entityName": table["name"],
                                  "schemaName": "dbo", "expressionSource": sm.EXPRESSION}


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


def test_aliases_filter_through_to_definitions(con):
    """One-directional would make every lookup by a spelling come back empty, which reads as
    'the term is not defined' rather than as a bug."""
    rels = {r["fromTable"]: r for r in _bim(con)["model"]["relationships"]}
    assert set(rels) == {"definitions", "aliases"}
    assert all(r["toTable"] == "terms" and r["toColumn"] == sm.KEY for r in rels.values())
    assert rels["aliases"]["crossFilteringBehavior"] == "bothDirections"
    assert "crossFilteringBehavior" not in rels["definitions"]


def test_the_ids_an_agent_needs_are_columns_of_the_fact(con):
    columns = {c["name"] for c in _table(_bim(con), "definitions")["columns"]}
    for needed in ("rank", "score", "name", "workspace_id", "owner_item_id"):
        assert needed in columns, needed


def test_the_dax_template_measure_is_on_the_fact(con):
    measures = _table(_bim(con), "definitions").get("measures") or []
    assert [m["name"] for m in measures] == ["Dax Template"]


def test_lineage_tags_are_stable_across_builds(con):
    """A second harvest updates this model. Random tags would churn every column's identity."""
    assert json.dumps(_bim(con), sort_keys=True) == json.dumps(_bim(con), sort_keys=True)


def test_definition_parts_carry_both_files_as_base64(con):
    parts = {p["path"]: p for p in sm.definition_parts(_bim(con))}
    assert set(parts) == {"model.bim", "definition.pbism"}
    for part in parts.values():
        assert part["payloadType"] == "InlineBase64"
        json.loads(base64.b64decode(part["payload"]).decode("utf-8"))


def test_a_local_store_creates_nothing(con):
    """The offline suite must stay offline: no workspace, no call, no failure."""
    class _Local:
        pass

    assert sm.ensure(con, _Local()) is None
