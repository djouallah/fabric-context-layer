"""The published context as a database - read by the benchmark, and by nothing else.

The client half of `ask` reads `Files/context.md` and runs DAX; it opens no database and
needs no driver (see `ask/context.py`). This module is what remains of the older query
side: it pulls the published Delta tables into DuckDB so `ask/evals.py` can build its
questions out of the graph itself. It is a maintainer tool. Nothing here answers a user's
question, and nothing here computes a number.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import duckdb

from .context import ROOT, LOCATION, CACHE_DIR, NotFound, is_remote  # noqa: F401

# What the query side pulls. `activity` is published too - it is the raw audit log the
# harvest derives item_usage from - but nothing here reads it, and it is by far the biggest
# table, so it is left in the lakehouse.
PUBLISHED = ("nodes", "edges", "terms", "definitions", "aliases", "item_usage",
             "query_usage", "query_stats", "meta", "flow", "measure_usage", "item_views")

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

class Ambiguous(Exception):
    def __init__(self, what: str, candidates: List[Dict]):
        super().__init__(what + " is ambiguous; candidates: "
                         + ", ".join(str(c.get("name")) + " (" + str(c.get("workspace")) + ")"
                                     for c in candidates[:6]))
        self.candidates = candidates


# ---------------------------------------------------------------- opening

def open_db(path: Optional[str] = None, refresh: bool = False, cache: bool = True):
    """A DuckDB connection over the published context, held locally.

    `path` is what the harvest returned - the Tables root of the lakehouse it
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
    if refresh or not os.path.exists(local):
        con = _pull(path)
        _save(con, local)
        con.close()
    return _with_views(duckdb.connect(local, read_only=True))


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
    from fabcontext._fabric import auth, delta, onelake

    options = (onelake.storage_options(auth.onelake_token())
               if path.startswith("abfss://") else None)
    con = duckdb.connect()
    for table in PUBLISHED:
        # An optional table simply does not arrive; every reader already degrades on that.
        delta.read_into(con, table, delta.table_url(path, "dbo", table), options)
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
        raise NotFound("nothing matches " + repr(key) + " in the published graph")
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
