-- Base tables. The whole context layer is two tables plus an event log; everything else in
-- this file is derived from them and is rebuilt from scratch on every build.

CREATE TABLE nodes (
    id           VARCHAR PRIMARY KEY,
    kind         VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    workspace    VARCHAR,
    item_id      VARCHAR,          -- the Fabric item this belongs to
    parent_id    VARCHAR,          -- model -> table -> measure
    description  VARCHAR,
    endorsement  VARCHAR,          -- 'Certified' | 'Promoted' | NULL
    owner        VARCHAR,
    modified_at  TIMESTAMP,
    attrs        JSON
);

CREATE TABLE edges (
    src     VARCHAR NOT NULL,
    dst     VARCHAR NOT NULL,
    rel     VARCHAR NOT NULL,
    weight  DOUBLE DEFAULT 1,
    attrs   JSON,
    PRIMARY KEY (src, rel, dst)
);

CREATE TABLE activity (
    event_id     VARCHAR PRIMARY KEY,
    ts           TIMESTAMP,
    activity     VARCHAR,
    user_id      VARCHAR,
    workspace_id VARCHAR,
    item_id      VARCHAR,
    item_kind    VARCHAR,
    attrs        JSON
);

-- What actually ran, from the workspace monitoring Eventhouse: one row per measure, and
-- one per semantic model in a monitored workspace. Both are empty unless the harvest was
-- given --query-log and the workspace has monitoring enabled. Counts only - the DAX text
-- stays in raw/ and is never published.
CREATE TABLE query_usage (
    def_id          VARCHAR PRIMARY KEY,   -- the measure node the query called
    item_id         VARCHAR,               -- the semantic model it was evaluated against
    workspace_id    VARCHAR,
    queries         BIGINT,
    report_queries  BIGINT,                -- from a Power BI client, i.e. a report visual
    adhoc_queries   BIGINT,                -- from a notebook, DAX Studio, Excel, XMLA
    users           BIGINT,                -- distinct users on the busiest identical query;
                                           -- a lower bound, not a model-wide distinct count
    last_queried    TIMESTAMP
);

CREATE TABLE query_stats (
    item_id              VARCHAR PRIMARY KEY,
    workspace_id         VARCHAR,
    workspace            VARCHAR,
    name                 VARCHAR,
    queries              BIGINT,
    texts                BIGINT,           -- distinct query texts behind those queries
    report_queries       BIGINT,
    adhoc_queries        BIGINT,
    measureless_queries  BIGINT,           -- called no measure: re-derived it inline, or
                                           -- the model was never parsed
    users                BIGINT,
    last_queried         TIMESTAMP,
    window_days          BIGINT,
    from_day             VARCHAR,
    to_day               VARCHAR
);

-- Data-flow direction, normalised so a lineage walk does not have to know which way each
-- relation was written. 'up' is upstream of 'down'.
-- 'contains' counts only where containment IS data flow: a measure's table carries the
-- table's source. A workspace or a lakehouse containing an item is organisational, and
-- following it would make every sibling look upstream of everything.
CREATE VIEW flow AS
    SELECT src AS up, dst AS down, rel FROM edges
     WHERE rel IN ('feeds', 'runs', 'refreshes')
        OR (rel = 'contains' AND (src LIKE 'model_table:%' OR src LIKE 'report:%'))
    UNION ALL
    SELECT dst AS up, src AS down, rel FROM edges
     WHERE rel IN ('sources_from', 'reads', 'references', 'uses');

-- A visual's measure references, carried up to the report that owns the visual.
CREATE VIEW measure_usage AS
    SELECT e.dst        AS def_id,
           v.item_id    AS report_id,
           count(*)     AS n_visuals
      FROM edges e
      JOIN nodes v ON v.id = e.src
     WHERE e.rel = 'references'
       AND v.kind = 'visual'
       AND (e.dst LIKE 'measure:%' OR e.dst LIKE 'column:%')
     GROUP BY 1, 2;

-- Build provenance, so a reader can say how fresh the context is and what is in scope.
CREATE TABLE meta (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR
);
