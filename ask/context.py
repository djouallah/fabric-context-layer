"""Read-only access to the published context, through duckrun.

The harvest side publishes the context into a Fabric lakehouse of its own and records the
address in context.json. This module opens it (or whatever `--db` names: a folder of Delta
tables, a `<workspace>/<lakehouse>.Lakehouse` shorthand, an abfss:// URL, a .duckdb file),
pulls the tables down once and queries them: nodes, edges, terms, definitions, flow,
measure_usage, item_views, plus the optional aliases, item_usage, meta.
No import from src/, no read of raw/ or build/, no write anywhere. Every public function
takes a connection and returns a plain dict, so a CLI, a skill or an MCP server can wrap
it without knowing DuckDB.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATION = os.path.join(ROOT, "context.json")
# What the query side pulls. `activity` is published too - it is the raw audit log the
# harvest derives item_usage from - but nothing here reads it, and it is by far the biggest
# table, so it is left in the lakehouse.
PUBLISHED = ("nodes", "edges", "terms", "definitions", "aliases", "item_usage",
             "query_usage", "query_stats", "meta", "flow", "measure_usage", "item_views")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Where the downloaded copy of a publish is kept - outside the repo, which holds no
# context. One file per publish; see cache_file().
CACHE_DIR = os.environ.get("FABRIC_CONTEXT_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "fabric-context")
LAST_CACHE: Optional[str] = None          # the copy the last open_db used, for `contract`


def location() -> Optional[Dict[str, Any]]:
    """Where the harvest side published the context. This file is the only thing the two
    sides share besides the tables themselves, and it holds an address, not content."""
    try:
        with open(LOCATION, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("path") else None


def default_db() -> str:
    where = location()
    if not where:
        raise NotFound("nothing published yet - no " + os.path.basename(LOCATION)
                       + ". The harvest side publishes the context into a Fabric lakehouse "
                       + "with: python src/run.py build --to \"<workspace>/<lakehouse>\"")
    return where["path"]


# Views the harvest side defines over its base tables. The published copy may carry them as
# tables; when it does not, they are recreated as session-local views at open.
DERIVED_VIEWS = {
    "flow": """
        SELECT src AS up, dst AS down, rel FROM edges
         WHERE rel IN ('feeds', 'runs', 'refreshes')
            OR (rel = 'contains' AND (src LIKE 'model_table:%' OR src LIKE 'report:%'))
        UNION ALL
        SELECT dst AS up, src AS down, rel FROM edges
         WHERE rel IN ('sources_from', 'reads', 'references', 'uses')""",
    "measure_usage": """
        SELECT e.dst AS def_id, v.item_id AS report_id, count(*) AS n_visuals
          FROM edges e JOIN nodes v ON v.id = e.src
         WHERE e.rel = 'references' AND v.kind = 'visual'
           AND (e.dst LIKE 'measure:%' OR e.dst LIKE 'column:%')
         GROUP BY 1, 2""",
    "item_views": """
        SELECT item_id, views, users AS viewers, last_seen AS last_viewed FROM item_usage""",
}

# The contract. Required tables must exist with these columns; optional ones widen what
# an answer can say and are used when present.
REQUIRED = {
    "nodes": ["id", "kind", "name", "workspace", "item_id", "parent_id", "description",
              "endorsement", "owner", "modified_at", "attrs"],
    "edges": ["src", "dst", "rel", "weight", "attrs"],
    "terms": ["term_id", "label", "n_definitions", "n_distinct_expr", "n_items",
              "conflicting", "top_def_id", "views", "n_reports"],
    "definitions": ["def_id", "term_id", "name", "kind", "workspace", "owner_item_id",
                    "owner_item_name", "table_name", "expression", "description",
                    "endorsement", "modified_at", "n_reports", "n_visuals", "views",
                    "authority", "popularity", "relevance", "freshness", "score", "rank",
                    "conflicting"],
    "flow": ["up", "down", "rel"],
    "measure_usage": ["def_id", "report_id", "n_visuals"],
    "item_views": ["item_id", "views"],
}
OPTIONAL = {
    "aliases": ["term_id", "alias", "alias_norm", "kind", "source_id"],
    "item_usage": ["item_id", "views", "runs", "queries", "refreshes", "users", "last_seen"],
    # Present only where the workspace has monitoring enabled and the harvest was run with
    # --query-log: what the DAX that actually ran called, per measure and per model.
    "query_usage": ["def_id", "item_id", "queries", "report_queries", "adhoc_queries",
                    "users", "last_queried"],
    "query_stats": ["item_id", "workspace", "queries", "measureless_queries", "window_days"],
    "meta": ["key", "value"],
}
SEARCH_KINDS = ("term", "measure", "report_measure", "semantic_model", "model_table",
                "column", "lakehouse_table", "lakehouse", "warehouse", "report", "notebook",
                "pipeline", "dashboard", "dataflow")
_KIND_ORDER = {"term": 0, "alias": 0, "measure": 1, "report_measure": 1, "semantic_model": 2,
               "model_table": 3, "lakehouse_table": 3, "column": 4}
SEARCH_THRESHOLD = 0.55       # below this, search reports nothing
LOOKUP_THRESHOLD = 0.7        # define / lineage / usage by name need a closer match


class NotFound(Exception):
    pass


class Ambiguous(Exception):
    def __init__(self, what: str, candidates: List[Dict]):
        super().__init__(what + " is ambiguous; candidates: "
                         + ", ".join(str(c.get("name")) + " (" + str(c.get("workspace")) + ")"
                                     for c in candidates[:6]))
        self.candidates = candidates


# ---------------------------------------------------------------- open, contract, scope

def open_db(path: Optional[str] = None, refresh: bool = False, cache: bool = True):
    """A DuckDB connection over the published context, held locally.

    `path` is a duckrun target - the `<workspace-guid>/<lakehouse-guid>` the harvest side
    recorded, `ws/name.Lakehouse`, an abfss:// URL, or a local folder of Delta tables.
    Omit it to read context.json. A .duckdb file still opens directly.

    OneLake charges about ten seconds a round trip and ninety to open the Delta logs, so
    reading the lakehouse afresh on every command would cost minutes for eight megabytes.
    The tables are pulled once and kept in a file named after the publish that produced
    them; the next command opens that in milliseconds, and the next publish changes the
    name, so a stale copy can never be read. The file lives outside the repo (see
    CACHE_DIR) - the repo keeps no context. `refresh` re-pulls; `cache=False` never
    writes one.

    This is the query side of the contract; the harvest side has its own copy of this
    logic and the two never share code."""
    global LAST_CACHE
    LAST_CACHE = None
    where = location()
    if not path:
        if not where:
            raise NotFound("nothing published yet - no " + os.path.basename(LOCATION)
                           + ". The harvest side publishes the context into a Fabric "
                           + "lakehouse with: python src/run.py build --to "
                           + "\"<workspace>/<lakehouse>\"")
        path = where["path"]
    elif not (where and where.get("path") == path):
        where = None                  # an explicit target we know nothing about: no copy
    if path.lower().endswith(".duckdb"):
        if not os.path.exists(path):
            raise NotFound("no context database at " + path)
        try:
            return _with_views(duckdb.connect(path, read_only=True))
        except duckdb.IOException as exc:
            raise SystemExit(os.path.basename(path) + " is locked (" + str(exc)[:160] + ")")
    if not is_remote(path) and not os.path.isdir(path):
        raise NotFound("no context at " + path + " - the harvest side has not published "
                       "one there (python src/run.py build --to ...)")
    if not (cache and where):
        return _with_views(_pull(path))
    local = cache_file(where)
    LAST_CACHE = local
    if refresh or not os.path.exists(local):
        con = _pull(path)
        _save(con, local)
        con.close()
    return _with_views(duckdb.connect(local, read_only=True))


def is_remote(path: str) -> bool:
    """The shapes duckrun expands to a OneLake URL, plus an explicit one. duckrun reads a
    bare `a/b` as a local relative path, so that is not one of them."""
    low = path.lower()
    if low.startswith(("abfss://", "az://", "s3://", "gs://")):
        return True
    parts = path.split("/")
    return len(parts) >= 2 and (parts[1].lower().endswith(".lakehouse")
                                or bool(_GUID.match(parts[0]) and _GUID.match(parts[1])))


def cache_file(where: Dict[str, Any]) -> str:
    """Where a copy of this publish lives. The publish timestamp is in the name, so a new
    publish is a new file rather than an invalidation anyone has to remember."""
    stamp = re.sub(r"[^0-9A-Za-z]", "", str(where.get("published_at") or "0"))
    return os.path.join(CACHE_DIR, str(where.get("lakehouse_id") or "context")
                        + "-" + stamp + ".duckdb")


def _save(con, local: str) -> None:
    """Write the pulled tables to `local`, then drop older copies of the same lakehouse."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = local + ".writing"
    for stale in (tmp, tmp + ".wal"):
        if os.path.exists(stale):
            os.remove(stale)
    con.execute("ATTACH '" + tmp.replace("'", "''") + "' AS copy_db")
    for table in _tables(con):
        con.execute("CREATE TABLE copy_db." + table + " AS SELECT * FROM " + table)
    con.execute("DETACH copy_db")
    os.replace(tmp, local)
    prefix = os.path.basename(local).split("-")[0] + "-"
    for name in os.listdir(os.path.dirname(local)):
        if name.startswith(prefix) and name != os.path.basename(local):
            try:
                os.remove(os.path.join(os.path.dirname(local), name))
            except OSError:                            # noqa: PERF203 - best effort
                pass


def _pull(path: str):
    """Copy the published tables out of the Delta store into a fresh in-memory DuckDB."""
    import contextlib
    import logging
    import sys

    import duckrun

    # duckrun announces the catalog it opened on stdout and through dbt's logger; --json
    # output has to stay parseable, so both go to stderr while the session opens.
    previous = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            session = duckrun.connect(path, read_only=True)
    finally:
        logging.disable(previous)
    con = duckdb.connect()
    try:
        for table in PUBLISHED:
            try:
                frame = session.con.execute("SELECT * FROM " + table).arrow()
            except Exception:                    # noqa: BLE001 - an optional table is absent
                continue
            con.register("pull_" + table, frame)
            con.execute("CREATE TABLE " + table + " AS SELECT * FROM pull_" + table)
            con.unregister("pull_" + table)
    finally:
        try:
            session.close()
        except Exception:                        # noqa: BLE001 - nothing to do about it
            pass
    if not _tables(con):
        raise NotFound("no published tables at " + path)
    return con


def _with_views(con):
    _ensure_views(con)
    return con


def _ensure_views(con) -> None:
    """Recreate the harvest's views as session-local views when the copy lacks them."""
    present = _tables(con)
    for name, sql in DERIVED_VIEWS.items():
        if name in present:
            continue
        try:
            con.execute("CREATE OR REPLACE TEMP VIEW " + name + " AS " + sql)
        except Exception:                              # noqa: BLE001 - a base table is missing
            pass


def _attrs(raw: Any) -> Dict:
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _tables(con) -> Dict[str, List[str]]:
    """{table: [columns]} for the published tables and views in the current catalog, plus
    the session-local views recreated at open."""
    out: Dict[str, List[str]] = {}
    for table, column in con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE (table_catalog = current_database() OR table_catalog = 'temp') "
            "AND table_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY table_name, ordinal_position").fetchall():
        out.setdefault(table, []).append(column)
    return out


def has_table(con, name: str) -> bool:
    return name in _tables(con)


def has_column(con, table: str, column: str) -> bool:
    return column in (_tables(con).get(table) or ())


def tiered(con) -> bool:
    """Whether the publish marks its long tail (nodes.tier, schema_version 4 and up).

    An older publish has no such column; everything it holds is then treated as tier 1,
    which is what it meant before the column existed."""
    return has_column(con, "nodes", "tier")


def meta(con) -> Dict[str, str]:
    if not has_table(con, "meta"):
        return {}
    return {k: v for k, v in con.execute("SELECT key, value FROM meta").fetchall()}


def contract(con, path: str = "") -> Dict[str, Any]:
    """Which parts of the contract this database honours."""
    present = _tables(con)

    def check(spec):
        out = {}
        for table, cols in spec.items():
            have = present.get(table)
            out[table] = {"present": have is not None,
                          "missing_columns": [c for c in cols if have is not None and c not in have]}
        return out

    required, optional = check(REQUIRED), check(OPTIONAL)
    m = meta(con)
    profiled = 0
    if "nodes" in present:
        profiled = con.execute("SELECT count(*) FROM nodes WHERE kind = 'column' "
                               "AND json_extract_string(attrs, '$.profile') IS NOT NULL").fetchone()[0]
    ok = all(v["present"] and not v["missing_columns"] for v in required.values())
    where = location() or {}
    return {"db": path, "ok": ok, "required": required, "optional": optional,
            "schema_version": m.get("schema_version"), "built_at": m.get("built_at") or _mtime(path),
            "profiled_columns": int(profiled),
            # nodes.tier arrived in schema_version 4; an older publish simply has no long
            # tail marked, and every reader here treats it as all tier 1.
            "tiered": tiered(con) if "nodes" in present else False,
            "tiers": json.loads(m["tiers"]) if m.get("tiers") else {},
            "lakehouse": where.get("lakehouse"), "workspace": where.get("workspace"),
            "published_at": where.get("published_at"),
            "local_copy": LAST_CACHE, "tables": sorted(present)}


def _mtime(path: str) -> Optional[str]:
    try:
        return dt.datetime.utcfromtimestamp(os.path.getmtime(path)).replace(
            microsecond=0).isoformat() + "Z (file time)"
    except OSError:
        return None


def scope(con, path: str = "") -> Dict[str, Any]:
    """What is in the context: when it was built, which workspaces, which models can be
    queried, which stores exist, and how much of each kind there is."""
    m = meta(con)
    workspaces = json.loads(m["workspaces"]) if m.get("workspaces") else [
        {"name": n, "id": i} for n, i in con.execute(
            "SELECT name, item_id FROM nodes WHERE kind = 'workspace' ORDER BY 1").fetchall()]
    # An empty auto-created model answers nothing, and there are more of them than there
    # are real ones. Count them, list the rest. See graph._tier on the harvest side.
    tail = " AND m.tier < 3" if tiered(con) else ""
    models = [dict(zip(("name", "item_id", "workspace", "workspace_id", "endorsement",
                        "storage_mode", "n_measures", "n_tables"), r)) for r in con.execute("""
        SELECT m.name, m.item_id, m.workspace, w.item_id, m.endorsement,
               json_extract_string(m.attrs, '$.storage_mode'),
               (SELECT count(*) FROM definitions d WHERE d.owner_item_id = m.item_id),
               (SELECT count(*) FROM nodes t WHERE t.parent_id = m.id AND t.kind = 'model_table')
          FROM nodes m
          LEFT JOIN nodes w ON w.kind = 'workspace' AND w.name = m.workspace
         WHERE m.kind = 'semantic_model'""" + tail + """
         ORDER BY 7 DESC, 1""").fetchall()]
    n_empty = con.execute(
        "SELECT count(*) FROM nodes WHERE kind = 'semantic_model'"
        + (" AND tier >= 3" if tiered(con) else " AND false")).fetchone()[0]
    # `n_used` is how many of a store's tables a model, notebook or pipeline actually
    # touches; the rest are inventory, queryable with `ask sql` but part of no lineage.
    used = ("(SELECT count(*) FROM nodes t WHERE t.parent_id = s.id AND t.tier < 3)"
            if tiered(con) else "(SELECT count(*) FROM nodes t WHERE t.parent_id = s.id)")
    stores = [dict(zip(("name", "kind", "item_id", "workspace", "workspace_id", "n_tables",
                        "n_used", "external"), r)) for r in con.execute("""
        SELECT s.name, s.kind, s.item_id, s.workspace,
               coalesce(w.item_id, json_extract_string(s.attrs, '$.workspace_id')),
               (SELECT count(*) FROM nodes t WHERE t.parent_id = s.id),
               """ + used + """,
               coalesce(TRY_CAST(json_extract_string(s.attrs, '$.external') AS BOOLEAN), false)
          FROM nodes s
          LEFT JOIN nodes w ON w.kind = 'workspace' AND w.name = s.workspace
         WHERE s.kind IN ('lakehouse', 'warehouse')
         ORDER BY 8, 7 DESC, 1""").fetchall()]
    counts = {k: n for k, n in con.execute(
        "SELECT kind, count(*) FROM nodes GROUP BY 1 ORDER BY 2 DESC").fetchall()}
    n_terms, n_conf = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE conflicting) FROM terms").fetchone()
    return {"built_at": m.get("built_at") or _mtime(path),
            "schema_version": m.get("schema_version"),
            "activity_window": {"from": m.get("activity_from"), "to": m.get("activity_to"),
                                "days": m.get("activity_window_days")},
            "workspaces": workspaces, "models": models, "stores": stores,
            "empty_models": n_empty,
            "terms": {"total": n_terms, "conflicting": n_conf}, "counts": counts,
            "optional_tables": {t: has_table(con, t) for t in OPTIONAL}}


# ---------------------------------------------------------------- search

_TOKEN = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN.split(str(text or "").lower()) if t]


def search(con, text: str, limit: int = 20, kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ranked hits for some words: exact name, near name (Jaro-Winkler), shared tokens, or a
    mention inside a description or a DAX expression."""
    query = " ".join(_tokens(text))
    tokens = _tokens(text)
    if not tokens:
        return {"query": text, "hits": []}
    wanted = tuple(kinds) if kinds else SEARCH_KINDS
    alias_sql = ""
    if has_table(con, "aliases"):
        alias_sql = """
            UNION ALL
            SELECT 'term:' || a.term_id AS id, 'alias' AS kind, a.alias AS name,
                   NULL AS workspace, NULL AS parent_id, '' AS text, 1 AS tier
              FROM aliases a"""
    sql = """
        WITH q AS (SELECT ? AS query, ?::VARCHAR[] AS tokens),
        cand AS (
            SELECT n.id, n.kind,
                   CASE WHEN n.kind = 'term' THEN replace(n.id[6:], '-', ' ') ELSE n.name END AS name,
                   n.workspace, n.parent_id,
                   lower(coalesce(n.description, '') || ' '
                         || coalesce(json_extract_string(n.attrs, '$.expression'), '')) AS text,
                   """ + ("n.tier" if tiered(con) else "1") + """ AS tier
              FROM nodes n
             WHERE n.kind IN (""" + ", ".join("?" for _ in wanted) + """)
            """ + alias_sql + """
        ), scored AS (
            SELECT c.*,
                   regexp_split_to_array(lower(c.name), '[^a-z0-9]+') AS toks,
                   len(list_intersect(regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), q.tokens)) AS shared,
                   CASE WHEN lower(c.name) = q.query THEN 1.0 ELSE 0.0 END AS s_exact,
                   -- a near-spelling counts only for a short query, or when a word is shared:
                   -- edit distance between two long, unrelated phrases is noise
                   CASE WHEN len(q.tokens) <= 2
                          OR len(list_intersect(regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), q.tokens)) > 0
                        THEN jaro_winkler_similarity(lower(c.name), q.query)
                             * sqrt(least(length(c.name), length(q.query))::DOUBLE
                                    / greatest(length(c.name), length(q.query), 1))
                        ELSE 0.0 END AS s_name,
                   len(list_intersect(regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), q.tokens))::DOUBLE
                     / greatest(len(list_distinct(list_concat(
                           regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), q.tokens))), 1) AS s_tokens,
                   -- every word of a short name appears in a longer question: "summary" in
                   -- "what feeds the summary table"
                   0.85 * len(list_intersect(regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), q.tokens))::DOUBLE
                     / greatest(len(list_filter(regexp_split_to_array(lower(c.name), '[^a-z0-9]+'), x -> x <> '')), 1)
                     AS s_cover,
                   CASE WHEN length(c.text) > 0 AND c.text LIKE '%' || q.query || '%' THEN 0.7 ELSE 0.0 END AS s_text
              FROM cand c CROSS JOIN q
        )
        SELECT id, kind, name, workspace, parent_id, tier,
               greatest(s_exact, s_name, s_tokens, s_cover, s_text) AS score,
               CASE WHEN s_exact = 1.0 THEN 'exact name'
                    WHEN greatest(s_tokens, s_cover) >= s_name AND greatest(s_tokens, s_cover) >= s_text
                         THEN 'shared words'
                    WHEN s_text >= s_name THEN 'mentioned in DAX or description'
                    ELSE 'similar name' END AS why
          FROM scored
         WHERE greatest(s_exact, s_name, s_tokens, s_cover, s_text) >= ?
         ORDER BY score DESC, name
         LIMIT ?"""
    params = [query, tokens] + list(wanted) + [SEARCH_THRESHOLD, limit * 4]
    rows = con.execute(sql, params).fetchall()

    labels = {t: l for t, l in con.execute("SELECT term_id, label FROM terms").fetchall()}
    best: Dict[str, Dict] = {}
    for nid, kind, name, workspace, parent_id, tier, score, why in rows:
        hit = best.get(nid)
        if hit and hit["score"] >= score:
            continue
        term_id = nid[5:] if nid.startswith("term:") else None
        best[nid] = {"id": nid, "kind": "term" if kind == "alias" else kind,
                     "name": labels.get(term_id, name) if term_id else name,
                     "matched": name, "workspace": workspace, "parent_id": parent_id,
                     "term_id": term_id, "score": round(float(score), 3), "why": why,
                     "tier": int(tier or 1)}
    # The tier breaks ties, it does not move the score: the thresholds a caller checks
    # mean the same thing as before. A table nothing reads can still be found by name -
    # it just does not outrank the one a model is built on.
    hits = sorted(best.values(),
                  key=lambda h: (-h["score"], h["tier"], _KIND_ORDER.get(h["kind"], 5),
                                 h["name"]))[:limit]
    parents = {h["parent_id"] for h in hits if h.get("parent_id")}
    if parents:
        names = {i: n for i, n in con.execute(
            "SELECT id, name FROM nodes WHERE id IN (" + ", ".join("?" for _ in parents) + ")",
            list(parents)).fetchall()}
        for h in hits:
            h["parent"] = names.get(h.pop("parent_id"))
    else:
        for h in hits:
            h.pop("parent_id", None)
    for h in hits:
        if h["kind"] in ("measure", "report_measure"):
            row = con.execute("SELECT term_id FROM definitions WHERE def_id = ?", [h["id"]]).fetchone()
            h["term_id"] = row[0] if row else None
    return {"query": text, "hits": hits}


# ---------------------------------------------------------------- define

def resolve_term(con, ref: str) -> str:
    """A term id from an id, a label, an alias, or the nearest search hit."""
    key = str(ref or "").strip()
    if con.execute("SELECT 1 FROM terms WHERE term_id = ?", [key]).fetchone():
        return key
    if key.startswith("term:") and con.execute(
            "SELECT 1 FROM terms WHERE term_id = ?", [key[5:]]).fetchone():
        return key[5:]
    row = con.execute("SELECT term_id FROM terms WHERE lower(label) = lower(?)", [key]).fetchone()
    if row:
        return row[0]
    if has_table(con, "aliases"):
        row = con.execute("SELECT term_id FROM aliases WHERE alias_norm = lower(?) LIMIT 1",
                          [key]).fetchone()
        if row:
            return row[0]
    row = con.execute("SELECT term_id FROM definitions WHERE lower(name) = lower(?) "
                      "ORDER BY score DESC LIMIT 1", [key]).fetchone()
    if row:
        return row[0]
    hits = search(con, key, limit=5, kinds=["term", "measure", "report_measure"])["hits"]
    hits = [h for h in hits if h.get("term_id") and h["score"] >= LOOKUP_THRESHOLD]
    if not hits:
        raise NotFound("no term matches " + repr(key) + " - try: python -m ask search "
                       + json.dumps(key))
    return hits[0]["term_id"]


def _workspace_ids(con) -> Dict[str, str]:
    return {n: i for n, i in con.execute(
        "SELECT name, item_id FROM nodes WHERE kind = 'workspace'").fetchall()}


def define(con, ref: str) -> Dict[str, Any]:
    """Every definition of a term, ranked, with what it takes to execute the top one."""
    term_id = resolve_term(con, ref)
    ws_ids = _workspace_ids(con)
    term = con.execute("SELECT term_id, label, n_definitions, n_distinct_expr, n_items, "
                       "conflicting, top_def_id, views FROM terms WHERE term_id = ?",
                       [term_id]).fetchone()
    aliases: List[str] = []
    if has_table(con, "aliases"):
        aliases = [a for (a,) in con.execute(
            "SELECT DISTINCT alias FROM aliases WHERE term_id = ? ORDER BY 1", [term_id]).fetchall()]
    cols = ["def_id", "rank", "score", "name", "kind", "workspace", "owner_item_id",
            "owner_item_name", "table_name", "expression", "description", "endorsement",
            "modified_at", "n_reports", "n_visuals", "views", "authority", "popularity",
            "relevance", "freshness", "conflicting"]
    # Only a context built from a monitored workspace has these; an older publish does not,
    # and asking for the column would fail rather than degrade.
    have = _tables(con).get("definitions") or []
    query_cols = [c for c in ("queries", "adhoc_queries", "query_users", "last_queried")
                  if c in have]
    cols = tuple(cols + query_cols)
    defs = []
    for row in con.execute("SELECT " + ", ".join(cols) + " FROM definitions WHERE term_id = ? "
                           "ORDER BY rank", [term_id]).fetchall():
        d = dict(zip(cols, row))
        d["score"] = round(float(d["score"]), 3)
        d["signals"] = {k: round(float(d.pop(k) or 0), 3)
                        for k in ("authority", "popularity", "relevance", "freshness")}
        d["modified_at"] = str(d["modified_at"]) if d["modified_at"] else None
        if query_cols:
            d["last_queried"] = str(d["last_queried"]) if d.get("last_queried") else None
            # Absent when the owning workspace has no monitoring: no evidence either way,
            # which is not the same as evidence of nobody asking.
            d["queried"] = {c: d.pop(c) for c in query_cols}
        d["model"] = {"item_id": d["owner_item_id"], "name": d["owner_item_name"],
                      "kind": "semantic_model" if d["kind"] == "measure" else "report",
                      "workspace": d["workspace"], "workspace_id": ws_ids.get(d["workspace"])}
        d["references"] = [{"id": i, "kind": k, "name": n} for i, k, n in con.execute(
            "SELECT e.dst, coalesce(n.kind, 'unresolved'), coalesce(n.name, e.dst) FROM edges e "
            "LEFT JOIN nodes n ON n.id = e.dst WHERE e.src = ? AND e.rel = 'references'",
            [d["def_id"]]).fetchall()]
        d["reports"] = [{"id": i, "name": n, "workspace": w, "visuals": v, "views": vw}
                        for i, n, w, v, vw in con.execute(
            "SELECT r.id, r.name, r.workspace, mu.n_visuals, coalesce(iv.views, 0) "
            "FROM measure_usage mu JOIN nodes r ON r.id = 'report:' || mu.report_id "
            "LEFT JOIN item_views iv ON iv.item_id = mu.report_id WHERE mu.def_id = ?",
            [d["def_id"]]).fetchall()]
        defs.append(d)
    return {"term_id": term[0], "label": term[1], "n_definitions": term[2],
            "n_distinct_expressions": term[3], "n_items": term[4], "conflicting": bool(term[5]),
            "top_def_id": term[6], "views": term[7], "aliases": aliases, "definitions": defs,
            "note": ("Definitions disagree: " + str(term[3]) + " distinct expressions. Rank "
                     "is not correctness - say which one you used." if term[5]
                     else "All definitions agree.")}


# ---------------------------------------------------------------- model

def resolve_model(con, ref: str) -> Dict[str, Any]:
    key = str(ref or "").strip()
    rows = con.execute("""
        SELECT m.id, m.item_id, m.name, m.workspace, w.item_id, m.endorsement,
               json_extract_string(m.attrs, '$.storage_mode'), m.description, m.modified_at
          FROM nodes m LEFT JOIN nodes w ON w.kind = 'workspace' AND w.name = m.workspace
         WHERE m.kind = 'semantic_model'
           AND (m.item_id = ? OR m.id = ? OR lower(m.name) = lower(?))""",
                       [key, key, key]).fetchall()
    if not rows:
        hits = search(con, key, limit=5, kinds=["semantic_model"])["hits"]
        if hits and hits[0]["score"] >= 0.8:
            return resolve_model(con, hits[0]["id"])
        raise NotFound("no semantic model matches " + repr(key) + " - see python -m ask scope")
    cands = [dict(zip(("id", "item_id", "name", "workspace", "workspace_id", "endorsement",
                       "storage_mode", "description", "modified_at"), r)) for r in rows]
    if len(cands) > 1:
        raise Ambiguous("model " + key, cands)
    m = cands[0]
    m["modified_at"] = str(m["modified_at"]) if m["modified_at"] else None
    return m


def model(con, ref: str, table: Optional[str] = None, brief: bool = False) -> Dict[str, Any]:
    """The schema pack an agent needs to write DAX against one model."""
    m = resolve_model(con, ref)
    ws_ids = _workspace_ids(con)
    tables = []
    for tid, tname, attrs, desc in con.execute(
            "SELECT id, name, attrs, description FROM nodes WHERE kind = 'model_table' "
            "AND parent_id = ? ORDER BY name", [m["id"]]).fetchall():
        if table and tname.lower() != table.lower():
            continue
        a = _attrs(attrs)
        entry: Dict[str, Any] = {"name": tname, "mode": a.get("mode"), "is_hidden": a.get("is_hidden"),
                                 "n_rows": a.get("n_rows"), "description": desc,
                                 "n_columns": a.get("n_columns"), "n_measures": a.get("n_measures")}
        src = con.execute("""
            SELECT t.id, t.name, json_extract_string(t.attrs, '$.schema'), json_extract_string(t.attrs, '$.store'), t.item_id,
                   coalesce(w.item_id, json_extract_string(l.attrs, '$.workspace_id')),
                   coalesce(TRY_CAST(json_extract_string(l.attrs, '$.external') AS BOOLEAN), false), json_extract_string(l.attrs, '$.via')
              FROM edges e JOIN nodes t ON t.id = e.dst
              LEFT JOIN nodes l ON l.id = t.parent_id
              LEFT JOIN nodes w ON w.kind = 'workspace' AND w.name = t.workspace
             WHERE e.src = ? AND e.rel = 'sources_from'""", [tid]).fetchall()
        entry["source"] = [dict(zip(("id", "table", "schema", "store", "lakehouse_id",
                                     "workspace_id", "external", "via"), r)) for r in src] or None
        columns = []
        for cname, cattrs, cdesc in con.execute(
                "SELECT name, attrs, description FROM nodes WHERE kind = 'column' "
                "AND parent_id = ? ORDER BY name", [tid]).fetchall():
            ca = _attrs(cattrs)
            if brief:
                columns.append(cname)
                continue
            col = {"name": cname, "data_type": ca.get("data_type"),
                   "is_hidden": ca.get("is_hidden"), "source_column": ca.get("source_column"),
                   "display_folder": ca.get("display_folder"), "description": cdesc}
            if ca.get("column_type") == "calculated":
                col["expression"] = ca.get("expression")
            if ca.get("profile"):
                col["profile"] = ca["profile"]
            columns.append({k: v for k, v in col.items() if v not in (None, "", [], {})})
        entry["columns"] = columns
        tables.append({k: v for k, v in entry.items() if v not in (None, "", [], {})})

    relationships = [dict(zip(("from_table", "from_column", "to_table", "to_column",
                               "is_active", "cross_filter"), r)) for r in con.execute("""
        SELECT f.name, json_extract_string(e.attrs, '$.from_column'), t.name, json_extract_string(e.attrs, '$.to_column'),
               coalesce(TRY_CAST(json_extract_string(e.attrs, '$.is_active') AS BOOLEAN), true), json_extract_string(e.attrs, '$.cross_filter')
          FROM edges e JOIN nodes f ON f.id = e.src JOIN nodes t ON t.id = e.dst
         WHERE e.rel = 'relates_to' AND f.parent_id = ?
         ORDER BY 1, 3""", [m["id"]]).fetchall()]
    measures = [dict(zip(("name", "table", "expression", "description", "term_id", "rank",
                          "score", "conflicting", "format_string", "is_hidden"), r))
                for r in con.execute("""
        SELECT d.name, d.table_name, d.expression, d.description, d.term_id, d.rank,
               round(d.score, 3), d.conflicting, json_extract_string(n.attrs, '$.format_string'),
               TRY_CAST(json_extract_string(n.attrs, '$.is_hidden') AS BOOLEAN)
          FROM definitions d LEFT JOIN nodes n ON n.id = d.def_id
         WHERE d.owner_item_id = ?
         ORDER BY d.table_name, d.name""", [m["item_id"]]).fetchall()]
    if table:
        measures = [x for x in measures if str(x["table"]).lower() == table.lower()]
    usage = None
    if has_table(con, "item_usage"):
        row = con.execute("SELECT views, runs, queries, refreshes, users, last_seen "
                          "FROM item_usage WHERE item_id = ?", [m["item_id"]]).fetchone()
        if row:
            usage = dict(zip(("views", "runs", "queries", "refreshes", "users", "last_seen"),
                             (row[0], row[1], row[2], row[3], row[4], str(row[5]))))
    m.pop("id", None)
    m["workspace_id"] = m.get("workspace_id") or ws_ids.get(m.get("workspace"))
    return dict(m, tables=tables, relationships=relationships, measures=measures, usage=usage,
                execute_with={"workspace_id": m["workspace_id"], "dataset_id": m["item_id"]})


# ---------------------------------------------------------------- table, values

def resolve_table(con, ref: str) -> Dict[str, Any]:
    key = str(ref or "").strip()
    rows = con.execute("""
        SELECT t.id, t.name, json_extract_string(t.attrs, '$.schema'), json_extract_string(t.attrs, '$.store'), t.item_id, t.workspace
          FROM nodes t WHERE t.kind = 'lakehouse_table'
           AND (t.id = ? OR lower(t.name) = lower(?)
                OR lower(coalesce(json_extract_string(t.attrs, '$.store'), '') || '.' || coalesce(json_extract_string(t.attrs, '$.schema'), '')
                         || '.' || t.name) = lower(?)
                OR lower(coalesce(json_extract_string(t.attrs, '$.schema'), '') || '.' || t.name) = lower(?))
         ORDER BY CASE WHEN t.id = ? THEN 0 ELSE 1 END""", [key, key, key, key, key]).fetchall()
    if not rows:
        raise NotFound("no lakehouse table matches " + repr(key)
                       + " - try store.schema.table, or python -m ask search")
    cands = [dict(zip(("id", "name", "schema", "store", "lakehouse_id", "workspace"), r))
             for r in rows]
    exact = [c for c in cands if c["id"] == key]
    if exact:
        return exact[0]
    if len(cands) > 1:
        raise Ambiguous("table " + key, cands)
    return cands[0]


def table(con, ref: str) -> Dict[str, Any]:
    t = resolve_table(con, ref)
    attrs = _attrs(con.execute("SELECT attrs FROM nodes WHERE id = ?", [t["id"]]).fetchone()[0])
    ws_ids = _workspace_ids(con)
    ext = con.execute("SELECT attrs FROM nodes WHERE id = ?", ["lakehouse:" + str(t["lakehouse_id"])]).fetchone()
    store_attrs = _attrs(ext[0]) if ext else {}

    def neighbours(rel: str, incoming: bool = True):
        col = "src" if incoming else "dst"
        other = "dst" if incoming else "src"
        return [{"id": i, "kind": k, "name": n} for i, k, n in con.execute(
            "SELECT e." + col + ", coalesce(n.kind, 'missing'), coalesce(n.name, e." + col + ") "
            "FROM edges e LEFT JOIN nodes n ON n.id = e." + col
            + " WHERE e." + other + " = ? AND e.rel = ?", [t["id"], rel]).fetchall()]

    models_using = [{"model_table": mt, "model": mn, "model_item_id": mi} for mt, mn, mi in con.execute(
        "SELECT t.name, m.name, m.item_id FROM edges e JOIN nodes t ON t.id = e.src "
        "JOIN nodes m ON m.id = t.parent_id WHERE e.dst = ? AND e.rel = 'sources_from'",
        [t["id"]]).fetchall()]
    columns = attrs.get("columns") or []
    stats, values, ndv = attrs.get("stats") or {}, attrs.get("values") or {}, attrs.get("n_distinct") or {}
    profiled = []
    for c in columns:
        entry = {"name": c.get("name"), "type": c.get("type")}
        st = stats.get(c.get("name")) or {}
        entry.update({k: st[k] for k in ("min", "max", "null_frac") if k in st})
        if c.get("name") in ndv:
            entry["n_distinct"] = ndv[c["name"]]
        if c.get("name") in values:
            entry["values"] = values[c["name"]]
        profiled.append(entry)
    return {"id": t["id"], "name": t["name"], "schema": t["schema"], "store": t["store"],
            "lakehouse_id": t["lakehouse_id"], "workspace": t["workspace"],
            "workspace_id": ws_ids.get(t["workspace"]) or store_attrs.get("workspace_id"),
            "external": bool(store_attrs.get("external")), "format": attrs.get("format"),
            "n_rows": attrs.get("n_rows"), "n_files": attrs.get("n_files"),
            "version": attrs.get("version"), "profiled_at": attrs.get("profiled_at"),
            "columns": profiled or None,
            "onelake_path": (store_attrs.get("onelake_path") or "") and
                            store_attrs["onelake_path"].rstrip("/") + "/" + str(t["schema"]) + "/" + t["name"],
            "written_by": neighbours("feeds"), "read_by": neighbours("reads"),
            "models_using": models_using}


def column_profile(con, model_ref: str, table_name: str, column: str) -> Optional[Dict[str, Any]]:
    """The harvested profile of one model column, or None when the harvest has none."""
    m = resolve_model(con, model_ref)
    row = con.execute("""
        SELECT c.attrs FROM nodes c JOIN nodes t ON t.id = c.parent_id
         WHERE c.kind = 'column' AND t.parent_id = ? AND lower(t.name) = lower(?)
           AND lower(c.name) = lower(?)""", [m["id"], table_name, column]).fetchone()
    if not row:
        raise NotFound("no column " + table_name + "[" + column + "] in model " + m["name"])
    prof = _attrs(row[0]).get("profile")
    if not prof:
        return None
    return dict(prof, table=table_name, column=column, source="harvest profile")


_SQL_OK = ("SELECT", "WITH", "FROM", "DESCRIBE", "SHOW", "SUMMARIZE", "PIVOT", "UNPIVOT",
           "EXPLAIN")
_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)


def stores(con) -> Dict[str, Dict[str, Any]]:
    """{lower store name: {name, item_id, workspace, workspace_id, kind}} for every
    harvested lakehouse and warehouse that is not an external stub."""
    ws_ids = _workspace_ids(con)
    out: Dict[str, Dict[str, Any]] = {}
    for name, kind, item_id, workspace, attrs in con.execute(
            "SELECT name, kind, item_id, workspace, attrs FROM nodes "
            "WHERE kind IN ('lakehouse', 'warehouse') ORDER BY name").fetchall():
        a = _attrs(attrs)
        if a.get("external") or not item_id:
            continue
        out.setdefault(str(name).lower(), {
            "name": name, "kind": kind, "item_id": item_id, "workspace": workspace,
            "workspace_id": ws_ids.get(workspace) or a.get("workspace_id")})
    return out


def attach(con, name: str) -> Dict[str, Any]:
    """Attach one harvested store's SQL analytics endpoint to this connection, read-only.

    This is the fallback for data a semantic model does not cover: DAX answers a question
    about a measure, T-SQL answers one about a table nothing has modelled yet.
    """
    from . import fabric
    known = stores(con)
    hit = known.get(str(name or "").strip().lower())
    if not hit:
        raise NotFound("no harvested lakehouse or warehouse called " + repr(name)
                       + " - have: " + ", ".join(sorted(s["name"] for s in known.values()))[:400])
    if not hit["workspace_id"]:
        raise NotFound("store " + hit["name"] + " has no workspace id in the context")
    host = fabric.sql_endpoint(hit["workspace_id"], hit["item_id"], hit["kind"])
    fabric.attach_store(con, hit["name"], hit["name"], host)
    return dict(hit, host=host)


def _mentioned_stores(con, body: str) -> List[str]:
    """Harvested store names the query qualifies a table with, so `select ... from
    coffee.dbo.sales` attaches `coffee` without being told to."""
    known = stores(con)
    words = {w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]*(?=\s*\.)', body or "")}
    words |= {w.lower() for w in re.findall(r'"([^"]+)"(?=\s*\.)', body or "")}
    return [known[w]["name"] for w in sorted(words) if w in known]


def raw_sql(con, query: str, max_rows: int = 100,
            attach_stores: Optional[List[str]] = None) -> Dict[str, Any]:
    """DuckDB SQL over the context tables themselves (nodes, edges, terms, definitions,
    aliases, item_usage, query_usage, flow, measure_usage, meta), read-only.

    A store qualified in the query - `coffee.dbo.contoso_sales` - is attached over its SQL
    analytics endpoint first, so the same statement can read the context and the data it
    describes. Numbers a measure defines still come from DAX: T-SQL is the fallback for
    tables no semantic model covers, not a way around a definition.
    """
    body = _COMMENT.sub(" ", query or "").strip().rstrip(";").strip()
    first = re.split(r"[\s(]+", body, 1)[0].upper() if body else ""
    if not body or ";" in body or first not in _SQL_OK:
        raise NotFound("only a single SELECT-shaped statement runs here; got: "
                       + (query or "").strip()[:60])
    max_rows = max(1, min(int(max_rows or 100), 10000))
    wanted = list(attach_stores or []) + _mentioned_stores(con, body)
    attached = []
    for name in dict.fromkeys(wanted):
        attached.append(attach(con, name)["name"])
    started = time.time()
    rel = con.execute(body)
    columns = [d[0] for d in rel.description] if rel.description else []
    fetched = rel.fetchmany(max_rows + 1)
    rows = [dict(zip(columns, r)) for r in fetched[:max_rows]]
    return {"query": body, "columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": len(fetched) > max_rows, "attached": attached,
            "elapsed_ms": int((time.time() - started) * 1000)}


# ---------------------------------------------------------------- lineage, usage

LINEAGE_SQL = """
WITH RECURSIVE walk AS (
    SELECT {near} AS near, {far} AS far, rel, 1 AS depth, [?, {far}] AS path
      FROM flow WHERE {near} = ?
    UNION ALL
    SELECT f.{near}, f.{far}, f.rel, w.depth + 1, list_append(w.path, f.{far})
      FROM flow f JOIN walk w ON f.{near} = w.far
     WHERE w.depth < ? AND NOT list_contains(w.path, f.{far})
)
SELECT w.far AS id,
       coalesce(n.kind, CASE WHEN w.far LIKE 'unresolved:%' THEN 'unresolved' ELSE 'missing' END) AS kind,
       coalesce(n.name, w.far) AS name, n.workspace, min(w.depth) AS depth, any_value(w.rel) AS rel,
       coalesce(TRY_CAST(json_extract_string(n.attrs, '$.external') AS BOOLEAN), false)
         OR coalesce(TRY_CAST(json_extract_string(p.attrs, '$.external') AS BOOLEAN), false) AS external
  FROM walk w LEFT JOIN nodes n ON n.id = w.far LEFT JOIN nodes p ON p.id = n.parent_id
 GROUP BY 1, 2, 3, 4, 7
 ORDER BY depth, kind, name
"""


def resolve_node(con, ref: str) -> str:
    key = str(ref or "").strip()
    if con.execute("SELECT 1 FROM nodes WHERE id = ?", [key]).fetchone():
        return key
    if key.startswith("term:") or con.execute("SELECT 1 FROM terms WHERE term_id = ?", [key]).fetchone():
        return "term:" + key.replace("term:", "")
    hits = [h for h in search(con, key, limit=3)["hits"] if h["score"] >= LOOKUP_THRESHOLD]
    if not hits:
        raise NotFound("nothing matches " + repr(key) + " - try python -m ask search")
    if len(hits) > 1 and hits[1]["score"] >= hits[0]["score"] - 0.02 and hits[1]["id"] != hits[0]["id"]:
        raise Ambiguous(key, hits)
    return hits[0]["id"]


def lineage(con, ref: str, direction: str = "up", depth: int = 12) -> Dict[str, Any]:
    nid = resolve_node(con, ref)
    if nid.startswith("term:"):
        nid = con.execute("SELECT top_def_id FROM terms WHERE term_id = ?", [nid[5:]]).fetchone()[0]
    near, far = ("down", "up") if direction == "up" else ("up", "down")
    rows = con.execute(LINEAGE_SQL.format(near=near, far=far), [nid, nid, depth]).fetchall()
    return {"node": nid, "direction": direction,
            "nodes": [dict(zip(("id", "kind", "name", "workspace", "depth", "rel", "external"), r))
                      for r in rows]}


def usage(con, ref: str) -> Dict[str, Any]:
    nid = resolve_node(con, ref)
    def_ids = [nid]
    term_id = None
    if nid.startswith("term:"):
        term_id = nid[5:]
        def_ids = [d for (d,) in con.execute(
            "SELECT def_id FROM definitions WHERE term_id = ?", [term_id]).fetchall()]
    marks = ", ".join("?" for _ in def_ids)
    reports = [dict(zip(("id", "name", "workspace", "visuals", "views"), r)) for r in con.execute(
        "SELECT r.id, r.name, r.workspace, sum(mu.n_visuals), max(coalesce(iv.views, 0)) "
        "FROM measure_usage mu JOIN nodes r ON r.id = 'report:' || mu.report_id "
        "LEFT JOIN item_views iv ON iv.item_id = mu.report_id "
        "WHERE mu.def_id IN (" + marks + ") GROUP BY 1, 2, 3 ORDER BY 5 DESC", def_ids).fetchall()]
    referenced_by = [dict(zip(("id", "kind", "name"), r)) for r in con.execute(
        "SELECT e.src, n.kind, n.name FROM edges e JOIN nodes n ON n.id = e.src "
        "WHERE e.rel = 'references' AND e.dst IN (" + marks + ") AND n.kind <> 'visual'",
        def_ids).fetchall()]
    mentions = []
    if term_id:
        mentions = [dict(zip(("id", "kind", "name", "hits"), r)) for r in con.execute(
            "SELECT e.src, n.kind, n.name, e.weight FROM edges e JOIN nodes n ON n.id = e.src "
            "WHERE e.rel = 'mentions' AND e.dst = ?", [nid]).fetchall()]
    downstream = [n for n in lineage(con, def_ids[0], "down", 4)["nodes"] if n["kind"] != "missing"]
    activity = None
    if has_table(con, "item_usage"):
        item = con.execute("SELECT item_id FROM nodes WHERE id = ?", [def_ids[0]]).fetchone()
        if item and item[0]:
            row = con.execute("SELECT views, runs, queries, refreshes, users, last_seen FROM item_usage "
                              "WHERE item_id = ?", [item[0]]).fetchone()
            if row:
                activity = dict(zip(("views", "runs", "queries", "refreshes", "users", "last_seen"),
                                    (row[0], row[1], row[2], row[3], row[4], str(row[5]))))
    return {"node": nid, "term_id": term_id, "reports": reports, "referenced_by": referenced_by,
            "mentioned_in": mentions, "downstream": downstream, "owner_activity_28d": activity}
