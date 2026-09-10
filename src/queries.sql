-- Verification and demo queries.  duckdb context.duckdb  then paste one in,
-- or:  python run.py query "<sql>"

-- ---------------------------------------------------------------- what got harvested
SELECT kind, count(*) AS n FROM nodes GROUP BY 1 ORDER BY 2 DESC;
SELECT rel, count(*) AS n, round(avg(weight), 1) AS avg_weight FROM edges GROUP BY 1 ORDER BY 2 DESC;

-- Every edge must land on a real node except the deliberate unresolved placeholders.
SELECT count(*) AS dangling
  FROM edges e LEFT JOIN nodes n ON n.id = e.dst
 WHERE n.id IS NULL AND e.dst NOT LIKE 'unresolved:%';

-- What the heuristics could not bind. A big number here is the thing to fix next.
SELECT split_part(id, '/', 1) AS kind, count(*) AS n
  FROM nodes WHERE id LIKE 'unresolved:%' GROUP BY 1 ORDER BY 2 DESC;

-- A semantic model with no tables means its TMSL was never fetched.
SELECT m.name, m.workspace FROM nodes m
 WHERE m.kind = 'semantic_model'
   AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = m.id AND e.rel = 'contains');

-- ---------------------------------------------------------------- the term layer
SELECT term_id, label, n_definitions, n_distinct_expr, n_items, views, conflicting
  FROM terms ORDER BY views DESC, n_definitions DESC LIMIT 20;

-- Every term whose definitions disagree - the point of the exercise.
SELECT term_id, n_definitions, n_distinct_expr, n_items
  FROM terms WHERE conflicting ORDER BY n_distinct_expr DESC;

-- One term, ranked, with the four signals exposed.
SELECT rank, name, owner_item_name, endorsement,
       round(authority, 2) AS authority, round(popularity, 2) AS popularity,
       round(relevance, 2) AS relevance, round(freshness, 2) AS freshness,
       round(score, 2) AS score, views, n_reports, left(expression, 70) AS dax
  FROM definitions WHERE term_id = 'revenue' ORDER BY rank;

-- The competing expressions side by side.
SELECT term_id, count(*) AS defs, list(DISTINCT left(expression_norm, 60)) AS expressions
  FROM definitions GROUP BY 1 HAVING count(DISTINCT expression_norm) > 1
 ORDER BY 2 DESC LIMIT 10;

-- ---------------------------------------------------------------- usage
SELECT n.name AS report, n.workspace, iv.views, iv.viewers, iv.last_viewed
  FROM item_views iv JOIN nodes n ON n.item_id = iv.item_id AND n.kind = 'report'
 ORDER BY iv.views DESC LIMIT 15;

-- Measures nothing looks at: defined, never referenced by a visual.
SELECT d.name, d.owner_item_name, d.term_id
  FROM definitions d WHERE d.n_reports = 0 ORDER BY d.owner_item_name, d.name LIMIT 30;

-- ---------------------------------------------------------------- lineage
-- Upstream of one node. Replace the id; get it from:
--   SELECT top_def_id FROM terms WHERE term_id = 'revenue';
WITH RECURSIVE walk AS (
    SELECT down AS near, up AS far, rel, 1 AS depth, [down, up] AS path
      FROM flow WHERE down = 'PUT-A-NODE-ID-HERE'
    UNION ALL
    SELECT f.down, f.up, f.rel, w.depth + 1, list_append(w.path, f.up)
      FROM flow f JOIN walk w ON f.down = w.far
     WHERE w.depth < 10 AND NOT list_contains(w.path, f.up)
)
SELECT n.kind, n.name, n.workspace, min(w.depth) AS depth
  FROM walk w JOIN nodes n ON n.id = w.far
 GROUP BY 1, 2, 3 ORDER BY depth;

-- Which physical tables feed the most measures.
SELECT t.name AS table_name, count(DISTINCT m.id) AS measures
  FROM edges s
  JOIN nodes t   ON t.id = s.dst AND t.kind = 'lakehouse_table'
  JOIN edges c   ON c.dst = s.src AND c.rel = 'contains'
  JOIN edges c2  ON c2.src = s.src AND c2.rel = 'contains'
  JOIN nodes m   ON m.id = c2.dst AND m.kind = 'measure'
 WHERE s.rel = 'sources_from'
 GROUP BY 1 ORDER BY 2 DESC LIMIT 15;

-- ---------------------------------------------------------------- hygiene
-- Undocumented measures on terms people actually use.
SELECT d.name, d.owner_item_name, d.views
  FROM definitions d
 WHERE coalesce(d.description, '') = '' AND d.views > 0
 ORDER BY d.views DESC LIMIT 20;

-- Reports defining their own measures instead of using the model's.
SELECT owner_item_name AS report, count(*) AS local_measures
  FROM definitions WHERE kind = 'report_measure' GROUP BY 1 ORDER BY 2 DESC;
