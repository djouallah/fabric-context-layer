"""The context's own semantic model: the ranking, queryable as DAX.

This is what a client reads, and the only thing. `answers` is one row per spelling of every
term with rank 1 already picked - the measure, the owning model's two ids, a ready-to-run
query, a confidence, the rivals - so an agent whose only tool is a DAX query needs one
equality filter and nothing else. `definitions`, `terms` and `aliases` are the ranking behind
it, for a user who named a model or asked to compare. Direct Lake over the published tables,
so a harvest refreshes what it serves with nothing republished on the client side.

It is a curated subset of what is published, not the tables as they are: `COLUMNS` names
every column the model exposes and what it means, and that description is what a
metadata-driven agent reads off the field list. `context.md` and `wiki/` carry everything.

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

# The four tables the model exposes. The line is what only the harvest can know: a ranking is
# derived from 28 days of query history, endorsement and usage, none of which an agent can see,
# so it is decided here and published. A model's own structure - its tables, columns and their
# values - is live state that the model answers about itself in one metadata call, so
# publishing it would only serve a stale copy: a column renamed after the last harvest reads
# back as confidently wrong, which is worse than not being there at all.
TABLES = ("answers", "terms", "definitions", "aliases")

# What each table is for, and every column it exposes with what it means. A published column
# not named here is deliberately not in the model; one named here but missing from an older
# publish is skipped rather than failing the build.
DESCRIPTIONS = {
    "answers": "One row per spelling of every business term, carrying the winning definition: "
               "the measure to call, the model that owns it and its two ids, a ready-to-run "
               "query, a confidence and the rivals. Filter alias_norm with the user's own "
               "wording; the row is the answer.",
    "terms": "One row per business term.",
    "definitions": "Every competing definition of every term, ranked. For a user who named a "
                   "model, or asked to compare; otherwise answers already holds rank 1.",
    "aliases": "Every spelling of every term. Related to terms both ways, so a filter on a "
               "spelling reaches definitions.",
}
COLUMNS = {
    "answers": [
        ("alias", "One spelling of a business term, as a measure or a person wrote it."),
        ("alias_norm", "That spelling lowercased. Filter on this with the user's own wording."),
        ("term_id", "The term this spelling belongs to."),
        ("label", "The term's display name: its winning measure's own name."),
        ("measure", "The rank-1 measure. Call it by this name; never re-derive its logic."),
        ("model", "The semantic model that owns the rank-1 measure."),
        ("model_id", "That model's id: the datasetid the DAX runs against."),
        ("workspace_id", "That model's workspace id: the groupid the DAX runs against."),
        ("table_name", "The table the measure sits on in its model, when it has one."),
        ("expression", "The measure's DAX, for the Sources block. Never a source of filter "
                       "or column names."),
        ("dax", "A ready-to-run query for the number with no filter. Wrap the measure in "
                "CALCULATE for one."),
        ("confidence", "high, medium or low: how clear the ranking was. Read it; do not "
                       "re-derive it."),
        ("score", "The rank-1 definition's score."),
        ("n_definitions", "How many definitions the term has in the tenant."),
        ("conflicting", "True when those definitions disagree on their DAX."),
        ("rivals", "The other definitions, ranked, as 'measure in model (rank n)'. Empty "
                   "when there are none."),
    ],
    "terms": [
        ("term_id", "The term's id, its words normalised."),
        ("label", "The term's display name: its winning measure's own name."),
        ("n_definitions", "How many definitions the term has in the tenant."),
        ("conflicting", "True when those definitions disagree on their DAX."),
    ],
    "definitions": [
        ("term_id", "The term this definition is of."),
        ("rank", "1 is the answer. The ranking is decided at harvest and is not to be redone."),
        ("name", "The measure's name. Call it by this name; never re-derive its logic."),
        ("owner_item_name", "The semantic model that owns the measure."),
        ("owner_item_id", "That model's id: the datasetid the DAX runs against."),
        ("workspace_id", "That model's workspace id: the groupid the DAX runs against."),
        ("table_name", "The table the measure sits on in its model, when it has one."),
        ("expression", "The measure's DAX, for the Sources block. Never a source of filter "
                       "or column names."),
        ("endorsement", "Certified, Promoted, or empty."),
        ("score", "The definition's score; higher ranks first."),
        ("confidence", "high, medium or low: how clear the ranking was. Read it; do not "
                       "re-derive it."),
    ],
    "aliases": [
        ("term_id", "The term this spelling belongs to."),
        ("alias", "One spelling of the term, as a measure or a person wrote it."),
        ("alias_norm", "That spelling lowercased. Filter on this with the user's own wording."),
    ],
}
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


def _column(table: str, name: str, duckdb_type: str, description: str) -> Dict[str, Any]:
    return {"name": name, "dataType": data_type(duckdb_type), "sourceColumn": name,
            "description": description, "lineageTag": _tag(table, name),
            "summarizeBy": "none"}


def _curated(table: str, cols: List[Tuple[str, str]]) -> List[Tuple[str, str, str]]:
    """[(name, duckdb_type, description)] for the columns of `table` the model exposes, in
    COLUMNS order, skipping any the built table does not have."""
    types = dict(cols)
    return [(name, types[name], description)
            for name, description in COLUMNS.get(table, []) if name in types]


def _table(table: str, cols: List[Tuple[str, str]], measures: Optional[List[Dict]] = None):
    out: Dict[str, Any] = {
        "name": table,
        "description": DESCRIPTIONS[table],
        "lineageTag": _tag(table),
        "columns": [_column(table, name, kind, description)
                    for name, kind, description in _curated(table, cols)],
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

    `schema` is {table: [(column, duckdb_type)]}; only `TABLES` are used, only the columns in
    `COLUMNS`, and a table missing from it is left out rather than emitted empty.
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
    model_id = ws.create_semantic_model(name, parts)
    if folder:
        # Before the reframe, so a tenant that cannot file the icon fails nothing that looks
        # like a framing failure. `move_item` swallows its own errors either way.
        ws.move_item(model_id, folder)
    # A Direct Lake model that has never been framed answers every query with "cannot find
    # table", so the deploy is not finished until the reframe is.
    ws.refresh_semantic_model(model_id)
    return model_id
