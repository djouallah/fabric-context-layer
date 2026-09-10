"""Step 3: build/*.jsonl -> an in-memory database -> the lakehouse, with the terms,
definitions and ranking derived.

The ranking is the whole point of a context layer. Genie calls it OntoRank, Cortex Sense
describes four signals - relevance, authority, popularity, freshness - and ranks conflicting
definitions the way web search ranks pages. This is that idea in about forty lines of SQL,
with the weights written down instead of hidden.

The weights are hand-picked, not learned. A popular wrong definition still ranks first.

Everything this file writes is the published side of the contract with ask/: the tables
nodes, edges, activity, query_usage, query_stats, terms, definitions, aliases, item_usage,
meta and the views flow, measure_usage, item_views. ask/ reads them and nothing else.

Nothing is written into the repo. `build()` returns an in-memory connection that
publish.py writes into the lakehouse; `open_published()` reads those tables back for the
steps that only look (wiki, viz, check, query, lineage), keeping a copy of each publish
outside the repo because OneLake charges seconds a round trip.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

import duckdb

from common import HERE, manual_aliases, read_text

# Where a downloaded copy of a publish is kept, outside the repo. See open_published.
CACHE_DIR = os.environ.get("FABRIC_CONTEXT_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "fabric-context")

# Signal weights. Change them here; every page in the wiki re-renders from the result.
W_AUTHORITY = 2.0
W_POPULARITY = 1.5
W_RELEVANCE = 1.0
W_FRESHNESS = 0.5
VIEW_WINDOW_DAYS = 28
FRESHNESS_HALFLIFE_DAYS = 180.0

# Bumped when a published table or column changes shape; ask/ checks it.
SCHEMA_VERSION = 3

# Activity families. Views are people opening things; runs and queries are people using
# them another way. The harvest's own calls and storage churn are noise, not usage.
VIEW_EVENTS = ("ViewReport", "ViewDashboard", "ViewTile", "ViewArtifact", "ReadArtifact",
               "ViewDataflow", "ViewSemanticModel", "ViewDataset")
RUN_EVENTS = ("RunArtifact", "StartRunNotebook", "RunNotebook", "RunPipeline")
QUERY_EVENTS = ("QueryAISkillDevelopmentEndpoint", "RunGraphQuery", "ExecuteQueries",
                "CreateSqlQueryFromSqlAnalyticsEndpointLakehouse", "GetPowerBIDataModel",
                "RequestCopilot", "ExportArtifact")
REFRESH_EVENTS = ("RefreshDataset", "RefreshArtifact")
NOISE_EVENTS = ("ItemDefinitionExported", "ListLakehouseTables", "GetDataArtifactTableDetails",
                "DeleteFileOrBlob", "CreateFile", "CreateDirectory", "CopyBlob",
                "ListDataAccessRoles")

_NODE_COLUMNS = {
    "id": "VARCHAR", "kind": "VARCHAR", "name": "VARCHAR", "workspace": "VARCHAR",
    "item_id": "VARCHAR", "parent_id": "VARCHAR", "description": "VARCHAR",
    "endorsement": "VARCHAR", "owner": "VARCHAR", "modified_at": "VARCHAR", "attrs": "VARCHAR",
}
_EDGE_COLUMNS = {"src": "VARCHAR", "dst": "VARCHAR", "rel": "VARCHAR", "weight": "DOUBLE",
                 "attrs": "VARCHAR"}
_ACTIVITY_COLUMNS = {"event_id": "VARCHAR", "ts": "VARCHAR", "activity": "VARCHAR",
                     "user_id": "VARCHAR", "workspace_id": "VARCHAR", "item_id": "VARCHAR",
                     "item_kind": "VARCHAR", "attrs": "VARCHAR"}
_QUERY_USAGE_COLUMNS = {"def_id": "VARCHAR", "item_id": "VARCHAR", "workspace_id": "VARCHAR",
                        "queries": "BIGINT", "report_queries": "BIGINT",
                        "adhoc_queries": "BIGINT", "users": "BIGINT",
                        "last_queried": "VARCHAR"}
_QUERY_STATS_COLUMNS = {"item_id": "VARCHAR", "workspace_id": "VARCHAR",
                        "workspace": "VARCHAR", "name": "VARCHAR", "queries": "BIGINT",
                        "texts": "BIGINT", "report_queries": "BIGINT",
                        "adhoc_queries": "BIGINT", "measureless_queries": "BIGINT",
                        "users": "BIGINT", "last_queried": "VARCHAR",
                        "window_days": "BIGINT", "from_day": "VARCHAR", "to_day": "VARCHAR"}


def _columns_clause(spec: Dict[str, str]) -> str:
    return "{" + ", ".join("'" + k + "': '" + v + "'" for k, v in spec.items()) + "}"


def _sql_list(values) -> str:
    return "(" + ", ".join("'" + v + "'" for v in values) + ")"


def _load(con, table: str, path: str, spec: Dict[str, str]) -> None:
    if not os.path.exists(path):
        return
    cols = ", ".join(
        ("TRY_CAST(" + k + " AS TIMESTAMP) AS " + k) if k in ("modified_at", "ts", "last_queried")
        else ("TRY_CAST(" + k + " AS JSON) AS " + k) if k == "attrs" else k
        for k in spec)
    con.execute(
        "INSERT INTO " + table + " SELECT " + cols + " FROM read_json('"
        + path.replace("\\", "/") + "', format='newline_delimited', columns="
        + _columns_clause(spec) + ")")


def build(build_dir: str):
    """Build the database from the parsed JSONL and derive the ranked layer.

    In memory, and nowhere else: the caller hands the connection to publish.py, which
    writes the tables into the lakehouse. Returns (connection, counts); the caller closes
    the connection when it is done with it."""
    con = duckdb.connect()
    con.execute(read_text(os.path.join(HERE, "schema.sql")))

    _load(con, "nodes", os.path.join(build_dir, "nodes.jsonl"), _NODE_COLUMNS)
    _load(con, "edges", os.path.join(build_dir, "edges.jsonl"), _EDGE_COLUMNS)
    _load(con, "activity", os.path.join(build_dir, "activity.jsonl"), _ACTIVITY_COLUMNS)
    _load(con, "query_usage", os.path.join(build_dir, "query_usage.jsonl"), _QUERY_USAGE_COLUMNS)
    _load(con, "query_stats", os.path.join(build_dir, "query_stats.jsonl"), _QUERY_STATS_COLUMNS)

    _derive(con)
    counts = {row[0]: row[1] for row in con.execute(
        "SELECT 'nodes', count(*) FROM nodes UNION ALL SELECT 'edges', count(*) FROM edges "
        "UNION ALL SELECT 'activity', count(*) FROM activity "
        "UNION ALL SELECT 'terms', count(*) FROM terms "
        "UNION ALL SELECT 'definitions', count(*) FROM definitions "
        "UNION ALL SELECT 'aliases', count(*) FROM aliases "
        "UNION ALL SELECT 'query_usage', count(*) FROM query_usage").fetchall()}
    return con, counts


def _derive(con) -> None:
    # --- usage source: 28 days of activity, bucketed by what people did --------------
    con.execute("""
        CREATE OR REPLACE TABLE item_usage AS
        SELECT item_id,
               any_value(item_kind)                                   AS item_kind,
               count(*) FILTER (WHERE activity IN """ + _sql_list(VIEW_EVENTS) + """)    AS views,
               count(*) FILTER (WHERE activity IN """ + _sql_list(RUN_EVENTS) + """)     AS runs,
               count(*) FILTER (WHERE activity IN """ + _sql_list(QUERY_EVENTS) + """)   AS queries,
               count(*) FILTER (WHERE activity IN """ + _sql_list(REFRESH_EVENTS) + """) AS refreshes,
               count(*)                                               AS events,
               count(DISTINCT user_id)                                AS users,
               max(ts)                                                AS last_seen
          FROM activity
         WHERE ts >= now() - INTERVAL """ + str(VIEW_WINDOW_DAYS) + """ DAY
           AND activity NOT IN """ + _sql_list(NOISE_EVENTS) + """
         GROUP BY 1""")
    # The older name, kept as a view so nothing that reads views breaks.
    con.execute("""
        CREATE OR REPLACE VIEW item_views AS
        SELECT item_id, views, users AS viewers, last_seen AS last_viewed FROM item_usage""")

    # --- one row per definition of a term, scored ------------------------------------
    con.execute("""
        CREATE OR REPLACE TABLE definitions AS
        WITH d AS (
            SELECT m.id                                   AS def_id,
                   replace(t.dst, 'term:', '')            AS term_id,
                   m.name, m.kind, m.workspace,
                   m.item_id                              AS owner_item_id,
                   coalesce(o.name, m.workspace)          AS owner_item_name,
                   json_extract_string(m.attrs, '$.table')                      AS table_name,
                   json_extract_string(m.attrs, '$.expression')                 AS expression,
                   json_extract_string(m.attrs, '$.expression_norm')            AS expression_norm,
                   m.description,
                   coalesce(m.endorsement, o.endorsement) AS endorsement,
                   coalesce(m.modified_at, o.modified_at) AS modified_at,
                   coalesce(m.owner, o.owner)             AS owner,
                   coalesce(TRY_CAST(json_extract_string(m.attrs, '$.exact_term') AS BOOLEAN), false) AS exact_term
              FROM edges t
              JOIN nodes m ON m.id = t.src
              LEFT JOIN nodes o
                     ON o.item_id = m.item_id
                    AND o.kind = CASE m.kind WHEN 'measure' THEN 'semantic_model'
                                             ELSE 'report' END
             WHERE t.rel = 'defines'
        ), u AS (
            SELECT mu.def_id,
                   count(DISTINCT mu.report_id)        AS n_reports,
                   sum(mu.n_visuals)                   AS n_visuals,
                   sum(coalesce(iv.views, 0))          AS views
              FROM measure_usage mu
              LEFT JOIN item_usage iv ON iv.item_id = mu.report_id
             GROUP BY 1
        ), q AS (
            -- The DAX that actually ran, per measure. Empty for a workspace without
            -- monitoring, which is why it is ADDED to the proxy rather than replacing it:
            -- with only some workspaces covered, replacing would zero the popularity of
            -- every definition nobody is watching and hand the ranking to whichever
            -- workspace happens to be monitored. Once every workspace in the context is
            -- covered - query_stats has a row for every semantic model - the views and
            -- owner_usage terms below are the ones to drop, because a report open is then
            -- being counted twice: once as a view, and again as the visuals' queries.
            SELECT def_id, queries, adhoc_queries, users AS query_users, last_queried
              FROM query_usage
        ), s AS (
            SELECT d.*,
                   coalesce(u.n_reports, 0) AS n_reports,
                   coalesce(u.n_visuals, 0) AS n_visuals,
                   coalesce(u.views, 0)     AS views,
                   -- the owning model's own usage: opened, queried, refreshed
                   CASE WHEN d.kind = 'measure'
                        THEN coalesce(ou.views, 0) + coalesce(ou.queries, 0)
                             + coalesce(ou.refreshes, 0) ELSE 0 END AS owner_usage,
                   -- authority: certified beats promoted beats nothing; a documented
                   -- measure beats an undocumented one; a model beats a report.
                   (CASE d.endorsement WHEN 'Certified' THEN 2.0
                                       WHEN 'Promoted'  THEN 1.0 ELSE 0.0 END)
                     + CASE WHEN length(coalesce(d.description, '')) > 0 THEN 0.5 ELSE 0 END
                     + CASE WHEN d.kind = 'measure' THEN 0.5 ELSE 0 END      AS authority,
                   -- popularity: how often the measure was actually evaluated (where the
                   -- query log covers it), plus how much the reports that use it are
                   -- opened and how much the model that defines it is opened and refreshed.
                   coalesce(q.queries, 0)                                  AS queries,
                   coalesce(q.adhoc_queries, 0)                            AS adhoc_queries,
                   coalesce(q.query_users, 0)                              AS query_users,
                   q.last_queried                                          AS last_queried,
                   ln(1 + coalesce(u.views, 0)
                        + coalesce(q.queries, 0)
                        + CASE WHEN d.kind = 'measure'
                               THEN coalesce(ou.views, 0) + coalesce(ou.queries, 0)
                                    + coalesce(ou.refreshes, 0) ELSE 0 END)  AS popularity,
                   -- relevance: how widely it is referenced, plus an exact name match.
                   ln(1 + coalesce(u.n_reports, 0))
                     + 0.25 * ln(1 + coalesce(u.n_visuals, 0))
                     + CASE WHEN d.exact_term THEN 0.5 ELSE 0 END            AS relevance,
                   -- freshness: decay on when the owning item last changed.
                   CASE WHEN d.modified_at IS NULL THEN 0.0
                        ELSE exp(-date_diff('day', d.modified_at, now())
                                 / """ + str(FRESHNESS_HALFLIFE_DAYS) + """) END AS freshness
              FROM d
              LEFT JOIN u USING (def_id)
              LEFT JOIN item_usage ou ON ou.item_id = d.owner_item_id
              LEFT JOIN q ON q.def_id = d.def_id
        )
        SELECT *,
               """ + str(W_AUTHORITY) + """ * authority
             + """ + str(W_POPULARITY) + """ * popularity
             + """ + str(W_RELEVANCE) + """ * relevance
             + """ + str(W_FRESHNESS) + """ * freshness                      AS score,
               row_number() OVER (PARTITION BY term_id
                                  ORDER BY """ + str(W_AUTHORITY) + """ * authority
                                         + """ + str(W_POPULARITY) + """ * popularity
                                         + """ + str(W_RELEVANCE) + """ * relevance
                                         + """ + str(W_FRESHNESS) + """ * freshness DESC,
                                           name)                             AS rank,
               count(DISTINCT expression_norm)
                 OVER (PARTITION BY term_id) > 1                             AS conflicting
          FROM s""")

    # --- the term layer ---------------------------------------------------------------
    con.execute("""
        CREATE OR REPLACE TABLE terms AS
        SELECT term_id,
               -- the winning measure's own name reads better than the normalised id,
               -- whose tokens are sorted so that word order cannot split a term.
               arg_max(name, score)                     AS label,
               count(*)                                 AS n_definitions,
               count(DISTINCT expression_norm)          AS n_distinct_expr,
               count(DISTINCT owner_item_id)            AS n_items,
               count(DISTINCT expression_norm) > 1      AS conflicting,
               arg_max(def_id, score)                   AS top_def_id,
               sum(views)                               AS views,
               max(n_reports)                           AS n_reports
          FROM definitions
         GROUP BY 1""")

    # --- every spelling of every term, so a lookup by any of them lands ---------------
    con.execute("""
        CREATE OR REPLACE TABLE aliases AS
        SELECT DISTINCT replace(e.dst, 'term:', '')  AS term_id,
               m.name                                 AS alias,
               lower(m.name)                          AS alias_norm,
               m.kind                                 AS kind,
               m.id                                   AS source_id
          FROM edges e JOIN nodes m ON m.id = e.src
         WHERE e.rel = 'defines'""")
    for tid, names in manual_aliases().items():
        for alias in names:
            con.execute("INSERT INTO aliases VALUES (?, ?, ?, 'manual', NULL)",
                        [tid, alias, alias.lower()])

    # Push the rank back onto the edge, so the graph itself carries "defines - ranked #1".
    con.execute("""
        UPDATE edges SET attrs = json_object('rank', d.rank,
                                             'score', round(d.score, 3),
                                             'conflicting', d.conflicting)
          FROM definitions d
         WHERE edges.src = d.def_id AND edges.rel = 'defines'""")

    _meta(con)
    con.execute("CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst)")
    con.execute("CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind)")


def _meta(con) -> None:
    """What a reader needs to say how fresh and how wide the context is."""
    workspaces = [{"name": n, "id": i, "harvested_at": h} for n, i, h in con.execute(
        "SELECT name, item_id, json_extract_string(attrs, '$.harvested_at') FROM nodes WHERE kind = 'workspace' "
        "ORDER BY name").fetchall()]
    window = con.execute("SELECT min(ts), max(ts) FROM activity").fetchone()
    n_nodes, n_edges, n_terms = con.execute(
        "SELECT (SELECT count(*) FROM nodes), (SELECT count(*) FROM edges), "
        "(SELECT count(*) FROM terms)").fetchone()
    rows = {
        "built_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "schema_version": str(SCHEMA_VERSION),
        "activity_from": str(window[0]) if window and window[0] else None,
        "activity_to": str(window[1]) if window and window[1] else None,
        "activity_window_days": str(VIEW_WINDOW_DAYS),
        "workspaces": json.dumps(workspaces),
        "n_nodes": str(n_nodes), "n_edges": str(n_edges), "n_terms": str(n_terms),
        "weights": json.dumps({"authority": W_AUTHORITY, "popularity": W_POPULARITY,
                               "relevance": W_RELEVANCE, "freshness": W_FRESHNESS}),
    }
    con.execute("DELETE FROM meta")
    for key, value in rows.items():
        con.execute("INSERT INTO meta VALUES (?, ?)", [key, value])


# ---------------------------------------------------------------- queries

LINEAGE_SQL = """
WITH RECURSIVE walk AS (
    SELECT {near} AS near, {far} AS far, rel, 1 AS depth, [?, {far}] AS path
      FROM flow WHERE {near} = ?
    UNION ALL
    SELECT f.{near}, f.{far}, f.rel, w.depth + 1, list_append(w.path, f.{far})
      FROM flow f JOIN walk w ON f.{near} = w.far
     WHERE w.depth < ? AND NOT list_contains(w.path, f.{far})
)
SELECT w.far                                        AS id,
       coalesce(n.kind, CASE WHEN w.far LIKE 'unresolved:%' THEN 'unresolved'
                             ELSE 'missing' END)    AS kind,
       coalesce(n.name, w.far)                      AS name,
       n.workspace,
       min(w.depth)                                 AS depth,
       any_value(w.rel)                             AS rel
  FROM walk w LEFT JOIN nodes n ON n.id = w.far
 GROUP BY 1, 2, 3, 4
 ORDER BY depth, kind, name
"""


def lineage(con, node_id: str, direction: str = "up", max_depth: int = 12) -> List[tuple]:
    """Everything upstream (or downstream) of a node, nearest first. An end the graph has
    no node for is reported as kind 'unresolved' or 'missing' rather than dropped."""
    near, far = ("down", "up") if direction == "up" else ("up", "down")
    sql = LINEAGE_SQL.format(near=near, far=far)
    return con.execute(sql, [node_id, node_id, max_depth]).fetchall()


def open_published(path: Optional[str] = None, refresh: bool = False):
    """The published context, opened for reading.

    `path` is a duckrun target - `<workspace-guid>/<lakehouse-guid>`, `ws/name.Lakehouse`,
    an abfss:// URL, or a local folder of Delta tables. Omit it to use the address the last
    publish recorded in context.json.

    OneLake costs about ninety seconds to open the Delta logs and ten a table after that,
    so the tables are pulled once into a file named after the publish that produced them
    and reopened from there. The file lives outside the repo (CACHE_DIR): the repo keeps
    no context, and a new publish makes a new name rather than a stale copy.

    This is the harvest side reading back what it published. ask/ does the same on its own
    side of the contract, deliberately without sharing this code."""
    import publish as publish_mod

    where = publish_mod.load_location()
    if not path:
        if not where:
            raise SystemExit("nothing published yet; the first build needs: "
                             + 'python src/run.py build --to "<workspace>/<lakehouse>"')
        path = where["path"]
    elif not (where and where.get("path") == path):
        where = None                  # an explicit target we know nothing about: no copy
    if not where:
        return pull(path)
    local = cache_file(where)
    if refresh or not os.path.exists(local):
        con = pull(path)
        _save_copy(con, local)
        con.close()
    return duckdb.connect(local, read_only=True)


def cache_file(where: Dict[str, Any]) -> str:
    """Where the copy of this publish lives. The publish timestamp is in the name, so a
    new publish is a new file rather than an invalidation anyone has to remember."""
    stamp = re.sub(r"[^0-9A-Za-z]", "", str(where.get("published_at") or "0"))
    return os.path.join(CACHE_DIR, str(where.get("lakehouse_id") or "context")
                        + "-" + stamp + ".duckdb")


def _save_copy(con, local: str) -> None:
    """Write the pulled tables to `local`, then drop older copies of the same lakehouse."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = local + ".writing"
    for stale in (tmp, tmp + ".wal"):
        if os.path.exists(stale):
            os.remove(stale)
    con.execute("ATTACH '" + tmp.replace("'", "''") + "' AS copy_db")
    for (table,) in con.execute("SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema IN ('main', 'temp')").fetchall():
        con.execute("CREATE TABLE copy_db." + table + " AS SELECT * FROM " + table)
    con.execute("DETACH copy_db")
    os.replace(tmp, local)
    folder, base = os.path.dirname(local), os.path.basename(local)
    prefix = base.split("-")[0] + "-"
    for name in os.listdir(folder):
        if name.startswith(prefix) and name != base:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:                            # noqa: PERF203 - best effort
                pass


# What a reader pulls back. `activity` is published - it is the raw audit log the ranking
# is derived from, and worth having in the lakehouse - but nothing reads it after the
# build, and it is by far the biggest table, so it stays there.
READBACK = ("nodes", "edges", "terms", "definitions", "aliases", "item_usage",
            "query_usage", "query_stats", "meta",
            "flow", "measure_usage", "item_views")


def pull(path: str, tables=READBACK):
    """Copy `tables` from a duckrun target into a fresh in-memory DuckDB."""
    import logging

    import duckrun

    if not os.path.isdir(path) and not _is_remote(path):
        raise SystemExit("no context at " + path + " - run build first")
    logging.getLogger("duckrun").setLevel(logging.WARNING)
    session = duckrun.connect(path, read_only=True)
    con = duckdb.connect()
    try:
        for table in tables:
            try:
                frame = session.con.execute("SELECT * FROM " + table).fetch_arrow_table()
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
    return con


def _is_remote(path: str) -> bool:
    """The two shapes duckrun expands to OneLake, plus an explicit URL. A bare `a/b` is a
    local relative path to duckrun, so it is not one of them."""
    low = path.lower()
    if low.startswith(("abfss://", "az://", "s3://", "gs://")):
        return True
    parts = path.split("/")
    return len(parts) >= 2 and (parts[1].lower().endswith(".lakehouse")
                                or bool(_GUID.match(parts[0]) and _GUID.match(parts[1])))


_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
