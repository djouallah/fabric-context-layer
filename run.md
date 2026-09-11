# Running fabric-context-layer

The operational side. The [README](README.md) has the idea.

Work happens in a folder outside the repo, one per context, under
`%LOCALAPPDATA%\fabric-context` (`FABRIC_CONTEXT_CACHE` moves it). `context.json` at the
repo root records which lakehouse the context went into; it is an address, not content,
and both sides read it.

The context is not another item. There is one per tenant, ranked per domain, built by the
platform and hidden; an agent never needs its address. This POC keeps it in a lakehouse
because that is the durable store it can write to, and `context.json` stands in for
discovery. The repo holds code, no data: one Python file per harvest step, a two-table
graph, the ranking in one SQL statement. It is meant to be read.

## The ranking

Measure names normalise to terms (`Average Price`, `Price_AVG`, `Avg Price` are one term).
Two measures on one term with different DAX is a conflict; every definition is scored:

| signal | weight | from |
|---|---|---|
| authority | 2.0 | certified 2, promoted 1; +0.5 documented; +0.5 in a model, not a report |
| popularity | 1.5 | `ln(1 + opens of its reports + query-log evaluations + its model's opens, queries, refreshes)`, over 28 days |
| relevance | 1.0 | `ln(1 + reports) + 0.25 ln(1 + visuals)`; +0.5 exact name |
| freshness | 0.5 | `exp(-days since the owner changed / 180)` |

Rank 1 is the definition; a number is always rank 1 run by name on its own model, never
re-derived. Weights are hand-picked, in `src/graph.py`. **Rank is not correctness** - a
popular, certified, wrong definition still wins, and every answer to a conflicting term
says so in one line.

## Run it

```powershell
az login --scope https://api.fabric.microsoft.com/.default   # once, if not in a Fabric notebook

python src/run.py harvest --to "My Workspace/context_layer" --workspace "My Workspace"
python src/run.py build            # -> Tables/, and Files/build
python src/run.py profile          # lakehouse columns, stats and values (optional)
python src/run.py build            # publish again, now with the profiles
python src/run.py wiki             # -> Files/wiki
python src/run.py viz              # -> Files/graph.html
python src/run.py check
python src/run.py files list       # what is under Files/ now
```

`src/run.py all --to "..." --workspace "..."` does every step in order, in one process, so
the chained steps use the graph it just built instead of downloading it again. `--to` is
needed once - after that `context.json` remembers it. Re-running `harvest` refetches only
what changed; `build` is a pure function of the working `raw/`.

`harvest --query-log` additionally reads each workspace's **workspace monitoring**
Eventhouse for the DAX that actually ran, and attributes it to the measures each query
called - the one signal that says a measure was *evaluated* rather than merely written
into a report. It is off by default: monitoring bills against the capacity, and a
workspace without it is skipped, so partial coverage adds to the popularity signal rather
than replacing it. Query text stays in `raw/`; only counts are published.

Every step pushes what it produced up to `Files/`; `--no-push` leaves it local. A push is a
diff against the last one - only files whose size or mtime changed go up, and files that
disappeared locally are deleted remotely - because each file is its own round trip, and
re-sending 900 unchanged wiki pages would take minutes. `python src/run.py files pull`
brings the whole working set back down on another machine.

`build` creates the lakehouse schema-enabled if it does not exist. The context then lives
in the tenant it describes: its SQL analytics endpoint answers T-SQL over `dbo.nodes`,
`dbo.edges`, `dbo.terms`, `dbo.definitions` and the rest, Power BI and notebooks can read
it, and a Fabric data agent can be pointed at it.

```sql
-- on the lakehouse's SQL analytics endpoint
SELECT term_id, label, n_definitions FROM dbo.terms WHERE conflicting = 1;
SELECT name, owner_item_name, rank, expression
  FROM dbo.definitions WHERE term_id = 'avg-price' ORDER BY rank;
```

## Ask it

```powershell
python -m ask contract                        # what the lakehouse publishes, and what is missing
python -m ask scope
python -m ask search "average price"
python -m ask define "average price"          # ranked, with the DAX and the conflict flag
python -m ask model <model> --table <table>   # schema pack, with filter values where profiled
python -m ask values <model> <table> <column> # distinct values and range, harvested or live
python -m ask table <store.schema.table>      # columns and profile, who writes and reads it
python -m ask dax <model> "EVALUATE ROW(\"v\", [<measure>])"
python -m ask lineage "<measure>"
python -m ask usage "<term>"
python -m ask sql "select ... from terms"     # DuckDB SQL over the context tables
```

`--json`, `--db`, `--refresh` and `--no-cache` are global flags and go before the
subcommand.

`sql` also reads the data itself when the query qualifies a table with a harvested store
(`select ... from coffee.benchmark_tests.contoso_sales`): the store attaches read-only over
its SQL analytics endpoint through DuckDB's `mssql` community extension, so context and
data join in one statement. It is the fallback for tables no semantic model covers; a
measure's number still comes from DAX. The dialect is DuckDB, not T-SQL.

Opening Delta tables over OneLake pays a round trip per file, so both sides download a
copy of the current publish to `%LOCALAPPDATA%\fabric-context`, named after the publish
that produced it. The first command after a build pays the pull; the rest take
milliseconds, and a new publish makes a new name rather than a stale copy. `--refresh`
re-pulls, `--no-cache` keeps none.

Any agent that can run a command line can drive it. The repo ships the protocol as a
Claude Code skill (`.claude/skills/fabric-context/SKILL.md`); another agent needs only the
same steps - search, define, model, values, dax - and to cite the ranked definition it
used. `wiki/` still opens in Obsidian; its `CLAUDE.md` explains both routes.

How a question flows:

1. `search` finds the term, by any of its spellings (`aliases`).
2. `define` returns every definition ranked, with the four signals, the DAX, and the
   dataset id to execute against. A conflicting term is reported as such.
3. `model` returns the tables, columns, relationships and source tables of the owning
   model; profiled columns carry their distinct values and ranges, so "NSW" becomes
   `'<table>'[<column>] = "NSW"` from the data, not from memory. `values` fetches them
   live when the harvest has not profiled the column.
4. `dax` runs a query that calls the ranked measure by name through the Power BI
   executeQueries API, so the governed logic runs rather than a re-derivation of it.
5. The answer cites the measure, model, rank, score, conflict note and the query that ran.

`python -m ask evals generate` derives a benchmark from the graph (definition, conflict,
lineage, usage and data questions for the top terms, plus one out-of-scope question);
`python -m ask evals run` runs it through Claude Code headless with and without the
context layer and reports accuracy per class. Nothing in it names a tenant.

## The nightly refresh

```powershell
python src/run.py deploy --daily 03:00 --tz "AUS Eastern Standard Time"
python src/run.py deploy --run          # redeploy and trigger one run now
```

`deploy` ships `src/` to `Files/code`, builds a **pure-Python** Fabric notebook, deploys it
with `updateDefinition` (so redeploying keeps the item id and its schedule) and PATCHes a
daily trigger. The notebook stages the code and `Files/raw` onto its own temp disk, runs
harvest -> build -> publish -> profile -> build -> wiki -> viz, and pushes the delta back.
It needs no `az login` and no attached lakehouse: duckrun resolves the Fabric notebook's own
identity, and every path is `abfss://`. `deploy --query-log` reads the monitoring
Eventhouse nightly too.

It is cheap night after night because the harvest is incremental where it counts: a
definition refetches only when its `lastUpdatedDate` moves, and the audit log is one file
per UTC day with only today and yesterday refetched. The scanner result and the store table
lists have no such signal, so they refetch once they are older than `--stale-after-days`
(default 1) - without that they would freeze on the first run and never move again.

The schedule runs as its owner, and the Scanner API and activity events need Fabric admin.
Without that, those two steps fail and the graph loses endorsement, cross-workspace
lineage and usage - the notebook prints a per-step table and exits non-zero so it is not
silent. If a run dies between `publish` and the push, `Tables/` is newer than `Files/` until
the next run; harmless.

## What it needs

| | |
|---|---|
| Python | 3.12, with `duckdb`, `duckrun` and `pyyaml` installed |
| Auth | Fabric admin, for the Scanner API and the audit log. A workspace member gets definitions and inventory but no endorsement, no lineage across workspaces and no usage. `dax` needs Build permission on the model and the tenant setting *Dataset Execute Queries REST API*. |
| Tenant settings | *Enhance admin API responses with detailed metadata* and *Enhance admin API responses with DAX and mashup expressions*. Without the second, the scanner returns measures with no expression - the harvest warns you. |
| Workspace monitoring | only for `--query-log`; enabled per workspace, bills against the capacity |

Everything reuses [duckrun](https://github.com/djouallah/duckrun) for the parts that are
genuinely hard: acquiring tokens for three different audiences, retrying through throttling,
polling the long-running definition endpoint, reading a Direct Lake partition's binding
back to its lakehouse table, and reading Delta tables on OneLake without Fabric compute.

## What is harvested

| Source | Gives |
|---|---|
| Item inventory (`/workspaces/{id}/items`) | every item and its type |
| Admin items | last modified and creator, for every item type |
| Scanner API | endorsement, report-to-model binding, upstream dataflows, datasource instances, cross-workspace lineage |
| `getDefinition` | TMSL for models, PBIR or PBIR-Legacy for reports, ipynb for notebooks, JSON for pipelines |
| Lakehouse tables | the physical tables at the bottom of the graph |
| Delta logs (`profile`) | columns, types, row counts and min/max per column from the Delta log (no data read); distinct values for low-cardinality string columns of model-bound tables, gated by `approx_count_distinct` so only genuinely small columns are listed |
| Activity events | 28 days of audit events: opens, runs, queries, refreshes, per user |
| Workspace monitoring (`--query-log`) | the DAX that ran, attributed to the measures it called; counts only, the query text stays in `raw/` |

## The graph

Two tables. `nodes(id, kind, name, workspace, item_id, parent_id, description, endorsement,
owner, modified_at, attrs)` and `edges(src, dst, rel, weight, attrs)`.

Node kinds: `workspace, semantic_model, model_table, column, measure, report, page, visual,
report_measure, dashboard, lakehouse, warehouse, lakehouse_table, notebook, pipeline,
dataflow, datasource, user, term, other_item, unresolved`. A lakehouse something binds to
but nobody harvested is a `lakehouse` node with `attrs.external = true`.

Relations: `contains, defines, references, sources_from, reads, feeds, runs, refreshes,
uses, relates_to, mentions, viewed_by, depends_on`.

A reference that cannot be bound to a harvested item still gets an edge, pointed at an
`unresolved:` id that deliberately has no node, so the miss is counted rather than dropped.
`src/run.py check` reports those separately from genuinely dangling edges.

Derived on top: `terms`, `definitions`, `aliases`, `item_usage` (with `item_views` kept as
a view), `query_usage`, `query_stats`, `meta`, and the views `flow` and `measure_usage`.

## Status

`python src/selftest.py` builds a synthetic tenant covering both report formats, a Direct
Lake model, a DirectQuery model, a model bound to an unharvested store, a notebook, a
pipeline, a profile file, an audit log and a query log, runs the whole pipeline over it
and checks the result - including the publish, the read-back and the query side, with a
temp folder standing in for the lakehouse. It prints how many checks it ran. No Fabric
access needed; run it after touching any parser.

The harvest has run against three real workspaces. Every REST call in `fabric_api.py` was
written from the documented contract, and none of them needed correcting. Failures are
caught per item and recorded in `raw/<workspace>/manifest.json` rather than stopping the run.

**`parse_report.py` has one real report of coverage.** Reports carry the popularity
signal; on a report-free workspace definitions are ordered on authority, model usage and
freshness.

## Files

| | |
|---|---|
| `src/common.py` | slugs, node ids, term normalisation, DAX stripping, the node/edge emitter |
| `src/fabric_api.py` | the REST calls, wrapped thin over duckrun |
| `src/harvest.py` | Fabric -> `raw/`, the query log included |
| `src/parse_model.py` | TMSL -> tables, measures, terms, DAX references, source bindings |
| `src/parse_report.py` | PBIR and PBIR-Legacy -> pages, visuals, field references |
| `src/parse_code.py` | notebooks and pipelines -> reads, feeds, runs |
| `src/parse.py` | orchestrates the parsers, the scanner, the audit log, the query log, external stubs, profiles |
| `src/profiling.py` | Delta logs and duckrun value scans -> `raw/*/profiles/` |
| `src/publish.py` | the context into a lakehouse, through duckrun, and `context.json` |
| `src/deploy.py`, `src/notebook.py` | the nightly refresh: ship the code, build the notebook, schedule it |
| `src/files.py` | the working files <-> the lakehouse `Files/` section, as a diff |
| `src/schema.sql`, `src/graph.py` | the database, the terms, the ranking, the lineage walk, the read-back |
| `src/wiki.py` | the markdown projection |
| `src/viz.py`, `src/graph_template.html` | the standalone `graph.html` |
| `src/queries.sql` | verification and demo SQL |
| `src/selftest.py` | the whole pipeline on a synthetic tenant |
| `context.json` | which lakehouse the context went into; the only thing besides the tables that both sides touch |
| `ask/context.py` | opening the published tables, and read-only queries over them |
| `ask/fabric.py` | live DAX, live column values, and the store attachment for `sql` |
| `ask/__main__.py` | `python -m ask` |
| `ask/evals.py` | the generated benchmark and its runner |
| `.claude/skills/fabric-context/SKILL.md` | how Claude Code uses `python -m ask` |

## Known limits

- **Notebook table references are regex heuristics.** A name built from a variable or an
  f-string is invisible. `%run` includes are not followed. SQL is only looked for inside
  `%%sql` cells and string literals, so `from x import y` is never a table.
- **Usage covers 28 days** of audit events. The audit log keeps 30.
- **Paginated reports** have no definition endpoint. Neither do lakehouses, warehouses, SQL
  endpoints, environments or KQL items - those are inventory-only nodes.
- **Warehouse views and stored procedures** need a SQL connection and are not harvested.
- **Dataflows Gen2** only expose a definition when CI/CD is enabled; otherwise only the
  scanner's metadata is available.
- **Term normalisation is a word-list**, not semantics. `Revenue` and `Net Sales` share no
  word and stay apart until `src/aliases.yaml` says otherwise. Sorting the words loses
  order: a term page lists every alias so a wrong merge is visible.
- **A model bound through a SQL endpoint outside the harvest** is a stub with the endpoint
  id only; its lakehouse and workspace stay unknown until that workspace is harvested.
- Only the workspaces you harvest are in the graph. A reference to anything outside them
  shows as unresolved or external.
