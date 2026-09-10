"""TMSL (model.bim) -> tables, columns, measures, terms, relationships, source bindings.

This is where the business definitions come from. A measure's DAX is the definition; its
name normalises to a term; two measures whose DAX differs on the same term are the
conflict the context layer exists to surface.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from duckrun.workspace import _M_TABLE_READ, _ONELAKE_REF, _SQL_DATABASE_REF

from common import (Emitter, is_exact_term, m_text, node_id, norm_dax, strip_dax,
                    term_id, unresolved_id)

# A qualified field reference: 'Sales Table'[Amount] or Sales[Amount].
_REF_QUOTED = re.compile(r"'([^']+)'\s*\[([^\[\]]+)\]")
_REF_BARE = re.compile(r"(?<![\w'\]\)])([A-Za-z_][\w]*)\s*\[([^\[\]]+)\]")
# An unqualified measure reference: [Total Revenue]. Measure names are unique model-wide,
# which is what makes this resolvable at all.
_REF_MEASURE = re.compile(r"(?<![\w\]'\)])\[([^\[\]]+)\]")
# Lakehouse.Contents-style M, and the lakehouseId a Fabric connector embeds.
_M_LAKEHOUSE_ID = re.compile(r'lakehouseId\s*=\s*\\?"([0-9a-fA-F-]{36})\\?"')
_M_WORKSPACE_ID = re.compile(r'workspaceId\s*=\s*\\?"([0-9a-fA-F-]{36})\\?"')


class ModelIndex:
    """Name -> node id lookups for one model, so a DAX reference can be resolved."""

    def __init__(self, model_guid: str):
        self.guid = model_guid
        self.columns: Dict[Tuple[str, str], str] = {}     # (table.lower, column.lower) -> id
        self.measures: Dict[str, str] = {}                # measure.lower -> id
        self.tables: Dict[str, str] = {}                  # table.lower -> id

    def column(self, table: str, field: str) -> Optional[str]:
        return self.columns.get((str(table).lower(), str(field).lower()))

    def measure(self, field: str) -> Optional[str]:
        return self.measures.get(str(field).lower())

    def table(self, name: str) -> Optional[str]:
        return self.tables.get(str(name).lower())


def extract_dax_refs(expr: str, index: ModelIndex) -> Dict[str, int]:
    """{node id: occurrences} for every measure and column a DAX expression references.

    Comments and string literals are blanked first, so a bracketed word inside either does
    not register as a reference.
    """
    text = strip_dax(expr)
    hits: Dict[str, int] = {}

    def bump(nid: Optional[str]) -> None:
        if nid:
            hits[nid] = hits.get(nid, 0) + 1

    seen_spans = []
    for pattern in (_REF_QUOTED, _REF_BARE):
        for match in pattern.finditer(text):
            table, field = match.group(1), match.group(2)
            seen_spans.append(match.span())
            bump(index.column(table, field) or index.measure(field)
                 or unresolved_id("dax", table + "[" + field + "]"))
    for match in _REF_MEASURE.finditer(text):
        if any(start <= match.start() < end for start, end in seen_spans):
            continue                                   # already counted as a qualified ref
        field = match.group(1)
        bump(index.measure(field) or unresolved_id("dax", "[" + field + "]"))
    return hits


def _resolve_expression_source(expr_text: str, sql_endpoint_to_lakehouse: Dict[str, str],
                               store_names: Dict[str, str]
                               ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(store item id, workspace id, how it was found) for the store an M shared expression
    points at.

    Direct Lake on OneLake names the workspace and item GUIDs in a OneLake URL. Direct Lake
    on SQL and DirectQuery go through Sql.Database, whose database is the SQL endpoint GUID
    (mapped back to its lakehouse when that lakehouse was harvested) or a warehouse name.
    An endpoint GUID nobody harvested is returned as-is, tagged 'sql_endpoint', so the
    caller can still say "bound to an external store" rather than dropping the edge.
    """
    onelake = _ONELAKE_REF.search(expr_text)
    if onelake:
        return onelake.group(2), onelake.group(1), "onelake"
    lake = _M_LAKEHOUSE_ID.search(expr_text)
    if lake:
        ws = _M_WORKSPACE_ID.search(expr_text)
        return lake.group(1), (ws.group(1) if ws else None), "lakehouse_id"
    sql = _SQL_DATABASE_REF.search(expr_text)
    if sql:
        db = sql.group("db")
        if db.lower() in sql_endpoint_to_lakehouse:
            return sql_endpoint_to_lakehouse[db.lower()], None, "sql_endpoint"
        if db.lower() in store_names:
            return store_names[db.lower()], None, "store_name"
        return db, None, "sql_endpoint"
    return None, None, None


def parse_model(bim: Dict, item: Dict, ws_name: str, g: Emitter,
                scanner_dataset: Optional[Dict] = None,
                sql_endpoint_to_lakehouse: Optional[Dict[str, str]] = None,
                store_names: Optional[Dict[str, str]] = None,
                modified_at: Optional[str] = None) -> ModelIndex:
    """Walk one model.bim, emitting its nodes and edges. Returns the index so callers
    (the report parser) can resolve field references against the same model."""
    sql_endpoint_to_lakehouse = sql_endpoint_to_lakehouse or {}
    store_names = store_names or {}
    scanner_dataset = scanner_dataset or {}

    guid = item["id"]
    model = (bim.get("model") or bim)
    name = item.get("displayName") or bim.get("name") or guid
    endorse = (scanner_dataset.get("endorsementDetails") or {}).get("endorsement")
    owner = scanner_dataset.get("configuredBy")
    model_id = node_id("semantic_model", guid)

    g.node(model_id, "semantic_model", name, workspace=ws_name, item_id=guid,
           description=item.get("description") or scanner_dataset.get("description"),
           endorsement=endorse, owner=owner, modified_at=modified_at,
           storage_mode=scanner_dataset.get("targetStorageMode"),
           certified_by=(scanner_dataset.get("endorsementDetails") or {}).get("certifiedBy"),
           compat_level=bim.get("compatibilityLevel"))

    index = ModelIndex(guid)
    tables = model.get("tables") or []

    # Pass 1: names, so DAX in pass 2 can resolve forward references.
    for table in tables:
        tname = table.get("name")
        if not tname:
            continue
        tid = node_id("model_table", guid, tname)
        index.tables[tname.lower()] = tid
        for col in table.get("columns") or []:
            if col.get("name"):
                index.columns[(tname.lower(), col["name"].lower())] = \
                    node_id("column", guid, tname, col["name"])
        for mea in table.get("measures") or []:
            if mea.get("name"):
                index.measures[mea["name"].lower()] = node_id("measure", guid, tname, mea["name"])

    # Pass 2: emit.
    expressions = {e.get("name"): m_text(e.get("expression"))
                   for e in (model.get("expressions") or []) if e.get("name")}

    for table in tables:
        tname = table.get("name")
        if not tname:
            continue
        tid = index.tables[tname.lower()]
        partitions = table.get("partitions") or []
        mode = next((p.get("mode") for p in partitions if p.get("mode")), None)
        g.node(tid, "model_table", tname, workspace=ws_name, item_id=guid, parent_id=model_id,
               description=table.get("description"), modified_at=modified_at,
               is_hidden=table.get("isHidden"), mode=mode,
               n_columns=len(table.get("columns") or []),
               n_measures=len(table.get("measures") or []))
        g.edge(model_id, tid, "contains")

        _emit_source(table, partitions, tid, guid, ws_name, expressions,
                     sql_endpoint_to_lakehouse, store_names, g)

        for col in table.get("columns") or []:
            cname = col.get("name")
            if not cname:
                continue
            cid = node_id("column", guid, tname, cname)
            g.node(cid, "column", cname, workspace=ws_name, item_id=guid, parent_id=tid,
                   description=col.get("description"), modified_at=modified_at,
                   data_type=col.get("dataType"), column_type=col.get("type"),
                   is_hidden=col.get("isHidden"), display_folder=col.get("displayFolder"),
                   source_column=col.get("sourceColumn"), table=tname,
                   expression=m_text(col.get("expression")) or None)
            g.edge(tid, cid, "contains")
            if col.get("type") == "calculated" and col.get("expression"):
                for ref, count in extract_dax_refs(m_text(col["expression"]), index).items():
                    g.edge(cid, ref, "references", weight=count, via="dax")

        for mea in table.get("measures") or []:
            _emit_measure(mea, tname, tid, guid, ws_name, index, endorse, owner,
                          modified_at, g)

    for rel in model.get("relationships") or []:
        from_t = index.table(rel.get("fromTable") or "")
        to_t = index.table(rel.get("toTable") or "")
        if from_t and to_t:
            g.edge(from_t, to_t, "relates_to",
                   from_column=rel.get("fromColumn"), to_column=rel.get("toColumn"),
                   cross_filter=rel.get("crossFilteringBehavior"),
                   is_active=rel.get("isActive", True))
    return index


def _emit_measure(mea: Dict, tname: str, tid: str, guid: str, ws_name: str,
                  index: ModelIndex, endorse: Optional[str], owner: Optional[str],
                  modified_at: Optional[str], g: Emitter) -> None:
    mname = mea.get("name")
    if not mname:
        return
    expr = m_text(mea.get("expression"))
    mid = node_id("measure", guid, tname, mname)
    tid_term = term_id(mname)
    g.node(mid, "measure", mname, workspace=ws_name, item_id=guid, parent_id=tid,
           description=mea.get("description"), endorsement=endorse, owner=owner,
           modified_at=modified_at, expression=expr, expression_norm=norm_dax(expr),
           display_folder=mea.get("displayFolder"), format_string=mea.get("formatString"),
           is_hidden=mea.get("isHidden"), table=tname,
           exact_term=is_exact_term(mname, tid_term))
    g.edge(tid, mid, "contains")

    term = node_id("term", tid_term)
    g.node(term, "term", tid_term.replace("-", " "), aliases=[mname])
    g.edge(mid, term, "defines")

    for ref, count in extract_dax_refs(expr, index).items():
        g.edge(mid, ref, "references", weight=count, via="dax")


def _emit_source(table: Dict, partitions: List[Dict], tid: str, guid: str, ws_name: str,
                 expressions: Dict[str, str], sql_endpoint_to_lakehouse: Dict[str, str],
                 store_names: Dict[str, str], g: Emitter) -> None:
    """Bind a model table to the physical table it reads - the seam between the graph layer
    and the relational layer. The edge carries the source workspace id when the M names
    one, so an unharvested store can still be located."""
    from duckrun.workspace import _partition_table

    tname = table.get("name")
    for part in partitions:
        src = part.get("source") or {}
        bound = _partition_table(part, tname)
        if not bound:
            text = m_text(src.get("expression"))
            if text:
                store, ws, via = _resolve_expression_source(text, sql_endpoint_to_lakehouse,
                                                            store_names)
                read = _M_TABLE_READ.search(text)
                if store and read:
                    g.edge(tid, node_id("lakehouse_table", store, read.group("schema"),
                                        read.group("item")), "sources_from",
                           mode=part.get("mode") or "import", source_workspace_id=ws,
                           source_via=via)
                elif text.strip():
                    g.edge(tid, unresolved_id("m_expression", (tname or "") + ":" + text[:80]),
                           "sources_from", mode=part.get("mode") or "import")
            continue
        schema, entity = bound
        store = ws = via = None
        expr_name = src.get("expressionSource")
        if expr_name and expr_name in expressions:
            store, ws, via = _resolve_expression_source(expressions[expr_name],
                                                        sql_endpoint_to_lakehouse, store_names)
        if not store:
            text = m_text(src.get("expression"))
            if text:
                store, ws, via = _resolve_expression_source(text, sql_endpoint_to_lakehouse,
                                                            store_names)
        if store:
            g.edge(tid, node_id("lakehouse_table", store, schema, entity), "sources_from",
                   mode=part.get("mode") or src.get("type"), source_workspace_id=ws,
                   source_via=via)
        else:
            g.edge(tid, unresolved_id("table", schema + "." + entity), "sources_from",
                   mode=part.get("mode") or src.get("type"))
