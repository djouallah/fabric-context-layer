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

Nothing is written to disk. `build()` returns an in-memory connection that publish.py
streams into the lakehouse, and `read_published()` reads those tables back for anything that
only looks. One run holds the graph it just built, so the read-back is for a later session,
not for the harvest itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List, Optional

import duckdb

from ._fabric import delta
from .common import HERE, manual_aliases, read_text

# Signal weights. Change them here; every page in the wiki re-renders from the result.
W_AUTHORITY = 2.0
W_POPULARITY = 1.5
W_RELEVANCE = 1.0
W_FRESHNESS = 0.5
VIEW_WINDOW_DAYS = 28
FRESHNESS_HALFLIFE_DAYS = 180.0

# Bumped when a published table or column changes shape; ask/ checks it.
SCHEMA_VERSION = 4

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
        "INSERT INTO " + table + " (" + ", ".join(spec) + ") SELECT " + cols
        + " FROM read_json('" + path.replace("\\", "/")
        + "', format='newline_delimited', columns=" + _columns_clause(spec) + ")")


def build(build_dir: str, aliases: Optional[Dict[str, List[str]]] = None):
    """Build the database from the parsed JSONL and derive the ranked layer.

    In memory, and nowhere else: the caller hands the connection to publish.py, which streams
    the tables into the lakehouse. `aliases` merges spellings the word lists cannot - "Net
    Sales" is "revenue" - as {term_id: [spelling, ...]}. Returns (connection, counts); the
    caller closes the connection when it is done with it."""
    con = duckdb.connect()
    con.execute(read_text(os.path.join(HERE, "schema.sql")))

    _load(con, "nodes", os.path.join(build_dir, "nodes.jsonl"), _NODE_COLUMNS)
    _load(con, "edges", os.path.join(build_dir, "edges.jsonl"), _EDGE_COLUMNS)
    _load(con, "activity", os.path.join(build_dir, "activity.jsonl"), _ACTIVITY_COLUMNS)
    _load(con, "query_usage", os.path.join(build_dir, "query_usage.jsonl"), _QUERY_USAGE_COLUMNS)
    _load(con, "query_stats", os.path.join(build_dir, "query_stats.jsonl"), _QUERY_STATS_COLUMNS)

    _derive(con, aliases)
    counts = {row[0]: row[1] for row in con.execute(
        "SELECT 'nodes', count(*) FROM nodes UNION ALL SELECT 'edges', count(*) FROM edges "
        "UNION ALL SELECT 'activity', count(*) FROM activity "
        "UNION ALL SELECT 'terms', count(*) FROM terms "
        "UNION ALL SELECT 'definitions', count(*) FROM definitions "
        "UNION ALL SELECT 'aliases', count(*) FROM aliases "
        "UNION ALL SELECT 'query_usage', count(*) FROM query_usage").fetchall()}
    return con, counts


def _purge_default_models(con) -> int:
    """Drop the semantic models Fabric made by itself and nobody ever built on.

    A default semantic model appears beside every lakehouse, carries whatever tables got
    synced into it, and defines nothing: no measure, so no definition, so no rank and no
    answer. It is not a demoted thing to be listed quietly - it is not a thing at all, and
    a tenant has one per lakehouse.

    Leaving them in is actively wrong, not merely noisy. Their tables `sources_from` the
    lakehouse tables underneath, and that edge is what marks a lakehouse table tier 1 - so
    one auto-synced default model promotes a whole sandbox lakehouse to load-bearing and
    the tier stops meaning anything. They also each get a section in `context.md` listing
    every column and no measure.

    The line is whether a person built on it, not whether Fabric made it: a measureless
    model that a report or a pipeline actually points at stays, demoted by `_tier`, because
    deleting it would break that report's lineage. Everything else goes, with its tables,
    its columns and every edge that touched them.
    """
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _junk AS
        WITH measureless AS (
            SELECT m.id
              FROM nodes m
             WHERE m.kind = 'semantic_model'
               AND m.item_id NOT IN (SELECT item_id FROM nodes
                                      WHERE kind = 'measure' AND item_id IS NOT NULL)
               -- nothing a person made points at it
               AND m.id NOT IN (SELECT dst FROM edges WHERE rel <> 'contains')
        )
        SELECT id FROM measureless
        UNION
        SELECT n.id FROM nodes n JOIN measureless m ON n.parent_id = m.id
        UNION
        SELECT c.id FROM nodes c
          JOIN nodes t ON c.parent_id = t.id
          JOIN measureless m ON t.parent_id = m.id""")
    dropped = con.execute("SELECT count(*) FROM _junk WHERE id LIKE 'semantic_model:%'"
                          ).fetchone()[0]
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _junk_items AS
        SELECT DISTINCT item_id FROM nodes
         WHERE id IN (SELECT id FROM _junk) AND item_id IS NOT NULL""")
    con.execute("DELETE FROM edges WHERE src IN (SELECT id FROM _junk)"
                "                     OR dst IN (SELECT id FROM _junk)")
    con.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM _junk)")
    # The monitoring log counts a default model like any other - a row saying nobody
    # queried the thing that could not have answered. It goes with the node.
    for table in ("activity", "query_usage", "query_stats"):
        con.execute("DELETE FROM " + table
                    + " WHERE item_id IN (SELECT item_id FROM _junk_items)")
    con.execute("DROP TABLE _junk_items")
    con.execute("DROP TABLE _junk")
    return dropped


def _derive(con, aliases: Optional[Dict[str, List[str]]] = None) -> None:
    # Before anything is derived: the nodes that should never have been in the graph.
    _purge_default_models(con)

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
    for tid, names in manual_aliases(aliases).items():
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

    _tier(con)
    _meta(con)
    con.execute("CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst)")
    con.execute("CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind)")


# Relations that make a table load-bearing, and relations that merely reach it. A store's
# `contains` is in neither list: being harvested is not the same as being used.
TIER1_RELS = ("sources_from",)
TIER2_RELS = ("reads", "feeds", "references", "depends_on", "uses")


def _tier(con) -> None:
    """Demote the long tail: what was harvested but nothing in the tenant refers to.

    The harvest stays wide on purpose - a lakehouse table list is one call per store, and
    an inventory that stops at what a model happens to use cannot say what is unused,
    where a table came from, or that a name exists at all. But most tables are read by
    nothing, and a wiki that gives each of them a page is mostly chaff. The tier says
    which is which; the renderers decide what to do about it, and nothing is deleted.

    Everything starts at tier 1 and is demoted from there, so a kind this function has
    never heard of keeps its page rather than silently vanishing.
    """
    # A lakehouse or warehouse table: 1 if a semantic model sources from it, 2 if code
    # reads or writes it, 3 if the only thing pointing at it is the store that holds it.
    con.execute(
        "UPDATE nodes SET tier = 3 WHERE kind = 'lakehouse_table'"
        "   AND id NOT IN (SELECT dst FROM edges WHERE rel IN "
        + _sql_list(TIER1_RELS + TIER2_RELS) + ")"
        "   AND id NOT IN (SELECT src FROM edges WHERE rel <> 'contains')")
    con.execute(
        "UPDATE nodes SET tier = 2 WHERE kind = 'lakehouse_table' AND tier = 1"
        "   AND id NOT IN (SELECT dst FROM edges WHERE rel IN "
        + _sql_list(TIER1_RELS) + ")")

    # A semantic model that defines no measure can answer nothing. The ones nobody built
    # on are already gone (see _purge_default_models); what reaches here is one a report
    # or a pipeline points at, so it is kept for that lineage and demoted, not deleted.
    con.execute(
        "UPDATE nodes SET tier = 3 WHERE kind = 'semantic_model'"
        "   AND item_id NOT IN (SELECT item_id FROM nodes"
        "                        WHERE kind = 'measure' AND item_id IS NOT NULL)")

    # A SQL endpoint is the plumbing under a lakehouse, never a thing anyone asks about.
    con.execute("UPDATE nodes SET tier = 3 WHERE kind = 'sql_endpoint'")

    # A column is only ever as interesting as the table under it. Last, so it picks up
    # every demotion above.
    con.execute(
        "UPDATE nodes SET tier = 3 WHERE kind = 'column'"
        "   AND parent_id IN (SELECT id FROM nodes WHERE tier = 3)")


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
        "built_at": dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0, tzinfo=None).isoformat() + "Z",
        "schema_version": str(SCHEMA_VERSION),
        "activity_from": str(window[0]) if window and window[0] else None,
        "activity_to": str(window[1]) if window and window[1] else None,
        "activity_window_days": str(VIEW_WINDOW_DAYS),
        "workspaces": json.dumps(workspaces),
        "n_nodes": str(n_nodes), "n_edges": str(n_edges), "n_terms": str(n_terms),
        # How much of the graph is the long tail, so a reader can say what it is not
        # being shown without counting it themselves. See _tier.
        "tiers": json.dumps({str(t): n for t, n in con.execute(
            "SELECT tier, count(*) FROM nodes GROUP BY 1 ORDER BY 1").fetchall()}),
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


# What a reader pulls back. `activity` is published - it is the audit log the ranking is
# derived from, and worth having in the lakehouse - but nothing reads it after the build and
# it is by far the biggest table, so it stays there.
READBACK = ("nodes", "edges", "terms", "definitions", "aliases", "item_usage",
            "query_usage", "query_stats", "meta",
            "flow", "measure_usage", "item_views")


def read_published(store, tables=READBACK):
    """The published context, read back into a fresh in-memory DuckDB.

    A table that is not there is skipped rather than raising: a context published by an older
    version simply arrives with fewer tables, and every reader already degrades on that.
    """
    from .publish import SCHEMA

    con = duckdb.connect()
    root, options = store.tables_root, store.storage_options
    for table in tables:
        delta.read_into(con, table, delta.table_url(root, SCHEMA, table), options)
    return con
