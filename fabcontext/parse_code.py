"""Notebooks and data pipelines -> what reads a table, what writes one, what runs what.

Notebook table references are recovered by regex over the code. This is a heuristic and it
is meant to be: a table name assembled from an f-string or a variable is invisible, and
that limitation is stated on every wiki page it affects. Pipelines are structured JSON and
are read exactly.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from ._fabric.patterns import walk_activities as _walk_activities

from .common import Emitter, GUID, node_id, unresolved_id

# --- table reads and writes, in two families ------------------------------------
# Code patterns match anywhere in a cell. SQL patterns match only inside SQL text - a
# %%sql cell, or the string literals of a Python cell - so `from deltalake import x` and
# `import shutil` can never read as a table.
_CODE_READ = [
    (re.compile(r"""spark\s*\.\s*(?:read\s*\.\s*)?table\(\s*["']([^"']+)"""), "spark.table"),
    (re.compile(r"""delta_scan\(\s*["']([^"']+)"""), "delta_scan"),
    (re.compile(r"""(?:read_delta|read_deltalake|DeltaTable)\(\s*["']([^"']+)"""), "delta read"),
]
_SQL_READ = [
    (re.compile(r"""\bFROM\s+(?!\()([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2})""", re.I), "sql from"),
    (re.compile(r"""\bJOIN\s+(?!\()([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2})""", re.I), "sql join"),
]
_CODE_WRITE = [
    (re.compile(r"""saveAsTable\(\s*["']([^"']+)"""), "saveAsTable"),
    (re.compile(r"""write_deltalake\(\s*["']([^"']+)"""), "write_deltalake"),
    (re.compile(r"""\.write\b[^\n]*?\.save\(\s*["']([^"']+)"""), "spark write"),
]
_SQL_WRITE = [
    (re.compile(r"""\b(?:INSERT\s+INTO|MERGE\s+INTO|OVERWRITE\s+TABLE)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2})""", re.I), "sql insert"),
    (re.compile(r"""\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2})""", re.I), "sql create"),
]
# Kept for callers that still import the old names.
_READ_PATTERNS = _CODE_READ + _SQL_READ
_WRITE_PATTERNS = _CODE_WRITE + _SQL_WRITE
# A cell that is SQL outright, and the string literals of one that is Python.
_SQL_MAGIC = re.compile(r"^\s*%%sql\b", re.I)
_STRING_LITERAL = re.compile(
    r'"""(.*?)"""'
    r"|'''(.*?)'''"
    r'|"((?:[^"\\n]|\.)*)"'
    r"|'((?:[^'\\n]|\.)*)'", re.S)


def _sql_text(code: str) -> str:
    """The parts of a cell that are SQL: the whole cell under %%sql, else its string
    literals joined - where spark.sql(...) and duckdb.sql(...) keep their queries."""
    if _SQL_MAGIC.match(code):
        return code
    return "\n".join(g for m in _STRING_LITERAL.finditer(code) for g in m.groups() if g)
# A OneLake table path names the workspace, the store and the table outright.
_ABFSS = re.compile(
    r"abfss://([^@\s\"']+)@onelake\.dfs\.fabric\.microsoft\.com/([^/\s\"']+)/Tables/"
    r"(?:([^/\s\"']+)/)?([^/\s\"'\)]+)")
# A notebook opening a store by path: connect("workspace/lakehouse.lakehouse/schema").
_CONNECT_PATH = re.compile(r"""connect\(\s*["']([^"']+)["']""")
# SQL keywords a bare FROM match must not capture.
_SQL_NOISE = {"select", "where", "group", "order", "having", "limit", "join", "on", "as",
              "values", "set", "when", "then", "else", "end", "union", "all", "by",
              "read_parquet", "read_csv", "read_json", "read_blob", "parquet_scan",
              "parquet_file_metadata", "delta_scan", "generate_series", "range", "dual",
              "glob", "information_schema", "duckdb_tables", "unnest"}
# A path names its store either by GUID or as "<display name>.<item type>".
_STORE_SUFFIX = re.compile(r"\.(?:lakehouse|warehouse|datawarehouse|kqldatabase|sqldatabase)$",
                           re.I)


class StoreIndex:
    """(store id, schema, table) lookups so a bare table name in code can be bound to a
    physical table - via the notebook's default lakehouse, or uniquely tenant-wide."""

    def __init__(self):
        self.by_store: Dict[Tuple[str, str, str], str] = {}
        self.by_name: Dict[str, Set[str]] = {}
        self.store_by_name: Dict[str, str] = {}
        self.store_ids: Set[str] = set()

    def add(self, store_id: str, schema: str, table: str, store_name: Optional[str] = None):
        nid = node_id("lakehouse_table", store_id, schema, table)
        self.by_store[(store_id, schema.lower(), table.lower())] = nid
        self.store_ids.add(store_id)
        self.by_name.setdefault(table.lower(), set()).add(nid)
        if store_name:
            self.store_by_name[store_name.lower()] = store_id
        return nid

    def store_id(self, segment: str) -> Optional[str]:
        """A path segment naming a store -> the harvested store's GUID, or None when the
        store was not harvested. The segment is the GUID outright, or the display name with
        an optional ".Lakehouse"-style suffix."""
        seg = str(segment).strip().strip("/")
        if GUID.fullmatch(seg):
            return seg if seg in self.store_ids else None
        return self.store_by_name.get(_STORE_SUFFIX.sub("", seg).lower())

    def bound(self, store: Optional[str], schema: str, table: str) -> Optional[str]:
        """The node id of a table already harvested from `store`, or None."""
        if not store:
            return None
        return self.by_store.get((store, schema.lower(), table.lower()))

    def resolve(self, raw: str, default_store: Optional[str],
                default_schema: str = "dbo") -> Optional[str]:
        """A name as written in code -> a table node id, or None when it cannot be bound."""
        parts = [p.strip().strip("`\"[]") for p in str(raw).split(".") if p.strip()]
        if not parts:
            return None
        if parts[-1].lower() in _SQL_NOISE:
            return None
        table = parts[-1]
        schema = parts[-2] if len(parts) >= 2 else default_schema
        store = default_store
        if len(parts) >= 3:
            store = self.store_by_name.get(parts[-3].lower(), store)
        if store:
            hit = self.by_store.get((store, schema.lower(), table.lower()))
            if hit:
                return hit
            hit = self.by_store.get((store, default_schema.lower(), table.lower()))
            if hit:
                return hit
        candidates = self.by_name.get(table.lower()) or set()
        if len(candidates) == 1:
            return next(iter(candidates))
        return None


def _cell_source(cell: Dict) -> str:
    src = cell.get("source")
    if isinstance(src, list):
        return "".join(str(line) for line in src)
    return str(src or "")


def parse_notebook(nb: Dict, item: Dict, ws_name: str, g: Emitter, stores: StoreIndex,
                   store_ids: Set[str], modified_at: Optional[str] = None,
                   owner: Optional[str] = None) -> None:
    guid = item["id"]
    nid = node_id("notebook", guid)
    deps = ((nb.get("metadata") or {}).get("dependencies") or {})
    lake = (deps.get("lakehouse") or {})
    default_store = lake.get("default_lakehouse")
    g.node(nid, "notebook", item.get("displayName") or guid, workspace=ws_name, item_id=guid,
           description=item.get("description"), owner=owner, modified_at=modified_at,
           default_lakehouse=lake.get("default_lakehouse_name"),
           n_cells=len(nb.get("cells") or []))
    if default_store:
        g.edge(nid, node_id("lakehouse", default_store), "uses", via="default lakehouse")

    reads: Dict[str, str] = {}
    writes: Dict[str, str] = {}
    unbound: Dict[str, str] = {}

    for cell in nb.get("cells") or []:
        if cell.get("cell_type") != "code":
            continue
        code = _cell_source(cell)
        if not code.strip():
            continue

        for match in _ABFSS.finditer(code):
            _ws, store_seg, schema, table = match.groups()
            schema = schema or "dbo"
            # The path segment is a display name, not a store id, and an f-string placeholder
            # is not a store at all - bind it or say so, never invent a table node id.
            target = (stores.bound(stores.store_id(store_seg), schema, table)
                      or unresolved_id("table", store_seg + "." + schema + "." + table))
            bucket = writes if re.search(r"write|save|MERGE|INSERT", code[:match.start()][-120:],
                                         re.I) else reads
            bucket[target] = "abfss path"

        for match in _CONNECT_PATH.finditer(code):
            bits = match.group(1).split("/")
            if len(bits) >= 2 and bits[1].lower().endswith(".lakehouse"):
                store = stores.store_by_name.get(bits[1][:-len(".lakehouse")].lower())
                if store:
                    default_store = default_store or store

        sql = _sql_text(code)
        for patterns, text, bucket in ((_CODE_READ, code, reads), (_SQL_READ, sql, reads),
                                       (_CODE_WRITE, code, writes), (_SQL_WRITE, sql, writes)):
            for pattern, label in patterns:
                for match in pattern.finditer(text):
                    raw = match.group(1)
                    target = stores.resolve(raw, default_store)
                    if target:
                        bucket[target] = label
                    elif raw.split(".")[-1].lower() not in _SQL_NOISE:
                        unbound[raw] = label

    for target, label in reads.items():
        if target not in writes:
            g.edge(nid, target, "reads", pattern=label)
    for target, label in writes.items():
        g.edge(nid, target, "feeds", pattern=label)
    for raw, label in unbound.items():
        g.edge(nid, unresolved_id("table", raw), "reads", pattern=label, heuristic=True)

    _guid_edges(str(nb), nid, store_ids, g)


def parse_pipeline(pipeline: Dict, item: Dict, ws_name: str, g: Emitter, stores: StoreIndex,
                   item_kind: Dict[str, str], modified_at: Optional[str] = None,
                   owner: Optional[str] = None) -> None:
    guid = item["id"]
    pid = node_id("pipeline", guid)
    activities = list(_walk_activities((pipeline.get("properties") or {}).get("activities")))
    g.node(pid, "pipeline", item.get("displayName") or guid, workspace=ws_name, item_id=guid,
           description=item.get("description"), owner=owner, modified_at=modified_at,
           n_activities=len(activities),
           activity_types=sorted({a.get("type") for a in activities if a.get("type")}))

    for act in activities:
        kind = act.get("type")
        tp = act.get("typeProperties") or {}
        if kind == "TridentNotebook" and tp.get("notebookId"):
            g.edge(pid, node_id("notebook", tp["notebookId"]), "runs", activity=act.get("name"))
        elif kind in ("InvokePipeline", "ExecutePipeline") and tp.get("pipelineId"):
            g.edge(pid, node_id("pipeline", tp["pipelineId"]), "runs", activity=act.get("name"))
        elif kind == "RefreshDataflow" and tp.get("dataflowId"):
            g.edge(pid, node_id("dataflow", tp["dataflowId"]), "refreshes",
                   activity=act.get("name"))
        elif kind in ("PBISemanticModelRefresh", "SemanticModelRefresh") and tp.get("datasetId"):
            g.edge(pid, node_id("semantic_model", tp["datasetId"]), "refreshes",
                   activity=act.get("name"))
        elif kind == "Copy":
            _copy_activity(tp, pid, stores, g, act.get("name"))

    _guid_edges(str(pipeline), pid, set(item_kind), g, item_kind)


def _copy_activity(tp: Dict, pid: str, stores: StoreIndex, g: Emitter,
                   activity: Optional[str]) -> None:
    for side, rel in (("source", "reads"), ("sink", "feeds")):
        settings = ((tp.get(side) or {}).get("datasetSettings") or {})
        props = settings.get("typeProperties") or {}
        table = props.get("table") or props.get("tableName")
        schema = props.get("schema") or "dbo"
        store = (((settings.get("linkedService") or {}).get("properties") or {})
                 .get("typeProperties") or {}).get("artifactId")
        if not table:
            continue
        if store:
            g.edge(pid, node_id("lakehouse_table", store, schema, table), rel,
                   activity=activity)
        else:
            g.edge(pid, unresolved_id("table", str(schema) + "." + str(table)), rel,
                   activity=activity, heuristic=True)


def _guid_edges(blob: str, src: str, known: Set[str], g: Emitter,
                item_kind: Optional[Dict[str, str]] = None) -> None:
    """Any GUID in a definition that matches a harvested item is a reference. Cheap, and it
    catches the couplings no dedicated parser knows about."""
    kinds = {"SemanticModel": "semantic_model", "Report": "report", "Notebook": "notebook",
             "DataPipeline": "pipeline", "Lakehouse": "lakehouse", "Warehouse": "warehouse",
             "Dataflow": "dataflow"}
    for guid in set(GUID.findall(blob)):
        if guid not in known or src.endswith(guid):
            continue
        kind = kinds.get((item_kind or {}).get(guid, ""), None)
        if kind:
            g.edge(src, node_id(kind, guid), "references", via="guid")
