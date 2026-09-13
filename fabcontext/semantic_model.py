"""The context's own semantic model: the ranking, queryable as DAX.

The published tables already are a star - `terms` is the dimension, `definitions` the fact of
one row per competing definition, `aliases` every spelling of every term - so this builds the
TMSL for a Direct Lake model over them and nothing is copied or reshaped.

Why it exists: an agent that can only run DAX (a Microsoft 365 Copilot agent, say, whose one
tool is the Power BI connector) has no way to read `context.md`. Give it this model and the
same tool that fetches a number also fetches the ranking - which is what keeps the ranked
answer out of a prompt that would otherwise have to carry the whole tenant.

`aliases` relates to `terms` **bidirectionally** on purpose: filters flow one-to-many, so a
lookup by a spelling on the many side would never reach `terms`, let alone propagate on to
`definitions`, and every query by an alias would come back empty.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# The three tables the model exposes. The rest of the published graph is not part of the
# question "which definition of this term wins", and a narrower model is a cheaper prompt.
TABLES = ("terms", "definitions", "aliases")
KEY = "term_id"
SCHEMA = "dbo"
DEFAULT_NAME = "context_model"
# The shared expression every entity partition resolves its table against.
EXPRESSION = "DirectLake"

# Stable lineage tags: a second harvest must update this model rather than churn every column
# into a new identity, so the tags are derived from the names instead of being random.
_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# DuckDB's type names to TMSL's. Anything unrecognised is a string, which is always readable
# and never wrong in a way that hides data.
_TYPES = (
    (("BOOLEAN",), "boolean"),
    (("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "UINTEGER"), "int64"),
    (("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC"), "double"),
    (("TIMESTAMP", "DATE", "DATETIME"), "dateTime"),
)


def _tag(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "/".join(parts)))


def data_type(duckdb_type: str) -> str:
    """The TMSL data type for a DuckDB column type."""
    upper = str(duckdb_type or "").upper()
    for names, tmsl in _TYPES:
        if any(upper.startswith(name) for name in names):
            return tmsl
    return "string"


def columns(con, table: str) -> List[Tuple[str, str]]:
    """[(name, duckdb_type)] for a built table, in declaration order."""
    return [(row[0], row[1]) for row in
            con.execute("DESCRIBE " + table).fetchall()]


def _column(table: str, name: str, duckdb_type: str) -> Dict[str, Any]:
    return {"name": name, "dataType": data_type(duckdb_type), "sourceColumn": name,
            "lineageTag": _tag(table, name), "summarizeBy": "none"}


def _table(table: str, cols: List[Tuple[str, str]], measures: Optional[List[Dict]] = None):
    out: Dict[str, Any] = {
        "name": table,
        "lineageTag": _tag(table),
        "columns": [_column(table, name, kind) for name, kind in cols],
        "partitions": [{
            "name": table,
            "mode": "directLake",
            "source": {"type": "entity", "entityName": table, "schemaName": SCHEMA,
                       "expressionSource": EXPRESSION},
        }],
    }
    if measures:
        out["measures"] = measures
    return out


def _relationship(from_table: str, to_table: str, both: bool = False) -> Dict[str, Any]:
    """Many-to-one, from the fact or bridge to the dimension - TMSL's `from` is the many side."""
    rel = {"name": _tag("rel", from_table, to_table), "fromTable": from_table,
           "fromColumn": KEY, "toTable": to_table, "toColumn": KEY}
    if both:
        rel["crossFilteringBehavior"] = "bothDirections"
    return rel


def _dax_template() -> Dict[str, Any]:
    """The ready-to-run query for the definition in filter context.

    An agent that copies this cannot re-derive a measure's logic by accident, which is the one
    failure mode where a wrong number comes back looking exactly like a right one.
    """
    return {
        "name": "Dax Template",
        "expression": ('"EVALUATE ROW(""v"", CALCULATE([" '
                       '& SELECTEDVALUE(\'definitions\'[name]) & "]))"'),
        "lineageTag": _tag("definitions", "measure", "Dax Template"),
    }


def bim(workspace_id: str, lakehouse_id: str,
        schema: Dict[str, List[Tuple[str, str]]],
        name: str = DEFAULT_NAME) -> Dict[str, Any]:
    """The model.bim for a Direct Lake on OneLake model over the context lakehouse.

    No SQL endpoint anywhere: every table is an entity partition resolved against one shared
    expression on the lakehouse's OneLake root.

    `schema` is {table: [(column, duckdb_type)]}; only `TABLES` are used, and a table missing
    from it is left out rather than emitted empty.
    """
    onelake = ("https://onelake.dfs.fabric.microsoft.com/" + workspace_id + "/"
               + lakehouse_id)
    tables = [_table(table, schema[table],
                     [_dax_template()] if table == "definitions" else None)
              for table in TABLES if schema.get(table)]
    present = {table["name"] for table in tables}
    relationships = [_relationship(many, "terms", both=(many == "aliases"))
                     for many in ("definitions", "aliases")
                     if many in present and "terms" in present]
    return {
        "name": name,
        "compatibilityLevel": 1604,          # directLake partitions and directLakeBehavior
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            # No DirectQuery fallback: a query this cannot serve should fail loudly rather
            # than quietly going slow against an endpoint the model is not supposed to use.
            "directLakeBehavior": "directLakeOnly",
            "expressions": [{
                "name": EXPRESSION,
                "kind": "m",
                "lineageTag": _tag("expression", EXPRESSION),
                # `HierarchicalNavigation` is what makes the OneLake root browsable, and
                # without it the model is created happily and then answers every query with
                # "cannot find table". The URL carries both GUIDs, which is also how this
                # repo's own parser recognises a Direct Lake model - see
                # _fabric/patterns.ONELAKE_REF.
                "expression": ["let",
                               '    Source = AzureStorage.DataLake("' + onelake
                               + '", [HierarchicalNavigation=true])',
                               "in",
                               "    Source"],
            }],
            "tables": tables,
            "relationships": relationships,
            "annotations": [{"name": "__fabcontext", "value": "the context layer's ranking"}],
        },
    }


def definition_parts(model: Dict[str, Any]) -> List[Dict[str, str]]:
    """The `definition.parts` payload the Fabric item APIs take."""
    def _part(path: str, body: Dict[str, Any]) -> Dict[str, str]:
        raw = json.dumps(body, indent=2).encode("utf-8")
        return {"path": path, "payload": base64.b64encode(raw).decode("ascii"),
                "payloadType": "InlineBase64"}

    return [_part("model.bim", model),
            _part("definition.pbism", {"version": "1.0", "settings": {}})]


_ABFSS = re.compile(r"^abfss://([^@/]+)@[^/]+/([^/]+)", re.I)


def ids_from_url(url: str) -> Tuple[str, str]:
    """(workspace_id, item_id) out of the URL `harvest()` returned, or a `<guid>/<guid>` pair.

    The model can be built over a lakehouse that was published long ago, so the ids have to be
    recoverable from the address alone - there is no state on the machine to look them up in.
    """
    match = _ABFSS.match(url or "")
    if match:
        return match.group(1), match.group(2)
    parts = [p for p in str(url or "").replace("\\", "/").split("/") if p]
    if len(parts) >= 2 and all(_GUID.match(p) for p in parts[:2]):
        return parts[0], parts[1]
    raise ValueError("cannot tell which workspace and lakehouse " + repr(url) + " names - it "
                     "is the abfss:// URL harvest() returned, or <workspace-guid>/<item-guid>")


def ensure(con, store, name: str = DEFAULT_NAME, folder: Optional[str] = None) -> Optional[str]:
    """Create or update the context model beside the tables, returning its id.

    Returns None when there is no tenant to create it in - a local store publishes the same
    Delta tables but has no workspace, and the offline suite must stay offline.
    """
    workspace_id = getattr(store, "workspace_id", None)
    item_id = getattr(store, "item_id", None)
    if not workspace_id or not item_id:
        return None
    from ._fabric import Workspace

    ws = Workspace(workspace_id)
    schema = {table: columns(con, table) for table in TABLES}
    parts = definition_parts(bim(workspace_id, item_id, schema, name))
    model_id = ws.create_semantic_model(name, parts, folder=folder)
    # A Direct Lake model that has never been framed answers every query with "cannot find
    # table", so the deploy is not finished until the reframe is.
    ws.refresh_semantic_model(model_id)
    return model_id
