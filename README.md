# fabric-context-layer

Harvest every asset in a Microsoft Fabric workspace, build a graph out of it, rank the
competing definitions of each business term, and answer questions from the result - with
the definition that ranks first executed against the model when the question needs a number.

The trend across data platforms in 2026 is the "context layer": context harvested
automatically from the assets you already have and ranked like web search, rather than a
model somebody authors and maintains. Fabric has not got one. This is the smallest honest
version, built from the metadata Fabric already exposes, in two parts that meet at one
Fabric item.

```
src/   harvest side                                  ask/   query side
Fabric -> Files/raw       every definition, the scanner result, 28 days of audit events
       -> Files/build     nodes and edges as jsonl
       -> Tables/         the ranked graph as Delta  =========>  reads context.json for the
       -> Files/wiki      one page per term and per item          address, opens the lakehouse
       -> Files/graph.html the whole graph in one page            with duckrun, runs DAX on
                          ... all in ONE Fabric lakehouse          the model for numbers
```

**The repo holds Python and no data.** One Fabric lakehouse holds the whole context: the
ranked graph as Delta tables under `Tables/`, and everything it was built from and rendered
into under `Files/` - the harvested JSON, the parsed edges, the wiki, the graph page.
`context.json` records which lakehouse. Work happens in a folder outside the repo, one per
context, under `%LOCALAPPDATA%\fabric-context` (`FABRIC_CONTEXT_CACHE` moves it).

The harvest side never knows the query side exists. The query side imports nothing from
`src/`; its only link is the published tables, and `python -m ask contract` checks the ones
it expects are there. It has two dependencies, duckrun to read the context and DAX to
compute numbers. `ask/README.md` spells the contract out.

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

Every step pushes what it produced up to `Files/`; `--no-push` leaves it local. A push is a
diff against the last one - only files whose size or mtime changed go up, and files that
disappeared locally are deleted remotely - because OneLake costs about 0.8 s a file and a
megabyte a second, so re-sending 900 unchanged wiki pages would take twelve minutes.
`python src/run.py files pull` brings the whole working set back down on another machine.

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
identity, and every path is `abfss://`.

It is cheap night after night because the harvest is incremental where it counts: a
definition refetches only when its `lastUpdatedDate` moves, and the audit log is one file
per UTC day with only today and yesterday refetched. The scanner result and the store table
lists have no such signal, so they refetch once they are older than `--stale-after-days`
(default 1) - without that they would freeze on the first run and never move again.

The schedule runs as its owner, and the Scanner API and activity events need Fabric admin.
Without that, those two steps fail and the graph silently loses endorsement, cross-workspace
lineage and usage - the notebook prints a per-step table and exits non-zero so it is not
silent. If a run dies between `publish` and the push, `Tables/` is newer than `Files/` until
the next run; harmless.


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

Reading it back over OneLake is slow in a way worth knowing: opening eleven Delta logs took
90 seconds against a real lakehouse and every table read another 10 - three minutes for
eight megabytes, because the cost is round trips, not bytes. So both sides download a copy
of the current publish to `%LOCALAPPDATA%\fabric-context`, named after the publish that
produced it. The first command after a build pays the pull; the rest take milliseconds, and
a new publish makes a new name rather than a stale copy. `python -m ask --refresh` re-pulls,
`--no-cache` keeps none.

Then ask:

```powershell
python -m ask scope
python -m ask search "average price"
python -m ask define "average price"           # ranked, with the DAX and the conflict flag
python -m ask model <model> --table <table>    # schema pack, with filter values where profiled
python -m ask dax <model> "EVALUATE ROW(\"v\", [<measure>])"
python -m ask lineage "<measure>"
python -m ask usage "<term>"
```

`--json`, `--db`, `--refresh` and `--no-cache` are global flags and go before the
subcommand.

Or open the repo in Claude Code and ask in words: the `fabric-context` skill follows the
same steps (search, define, model, values, dax) and cites the ranked definition it used.
`wiki/` still opens in Obsidian; its `CLAUDE.md` explains both routes.

## What the query side does with the context

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

## Status

The parse, graph and wiki steps are verified. `python src/selftest.py` builds a synthetic
tenant covering both report formats, a Direct Lake model, a DirectQuery model, a model bound
to an unharvested store, a notebook, a pipeline, a profile file and an audit log, runs the
whole pipeline over it and asserts 52 things - including the publish, the read-back and
the query side, with a temp folder standing in for the lakehouse. No Fabric access needed;
run it after touching any parser.

The harvest has run against three real workspaces. Every REST call in `fabric_api.py` was
written from the documented contract, and none of them needed correcting. Failures are
caught per item and recorded in `raw/<workspace>/manifest.json` rather than stopping the run.

**`parse_report.py` has one real report of coverage.** Reports carry the popularity
signal; on a report-free workspace definitions are ordered on authority, model usage and
freshness.

## What it needs

| | |
|---|---|
| Python | 3.12, with `duckdb`, `duckrun` and `pyyaml` installed |
| Auth | Fabric admin, for the Scanner API and the audit log. A workspace member gets definitions and inventory but no endorsement, no lineage across workspaces and no usage. `dax` needs Build permission on the model and the tenant setting *Dataset Execute Queries REST API*. |
| Tenant settings | *Enhance admin API responses with detailed metadata* and *Enhance admin API responses with DAX and mashup expressions*. Without the second, the scanner returns measures with no expression - the harvest warns you. |

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
a view), `meta`, and the views `flow` and `measure_usage`.

## The ranking

Each measure's name normalises to a term: stop words dropped, common synonyms folded
(`average`, `avg`, `mean`), plurals stripped, and the remaining words **sorted**, so
`Average Price`, `Price_AVG` and `Avg Price` are one term. `src/aliases.yaml` (optional)
pins merges the word lists cannot make. When two measures define the same term with
different DAX, that is a conflict, and the definitions are ranked on four signals:

| signal | weight | how it is computed |
|---|---|---|
| authority | 2.0 | certified 2, promoted 1; +0.5 documented; +0.5 defined in a model rather than a report |
| popularity | 1.5 | `ln(1 + 28-day opens of the reports that use it + opens, queries and refreshes of the model that defines it)` |
| relevance | 1.0 | `ln(1 + reports) + 0.25 ln(1 + visuals)`; +0.5 if the name is exactly the term |
| freshness | 0.5 | `exp(-days since the owning item changed / 180)` |

The weights are in `graph.py` and are hand-picked, not learned. **Rank is not correctness.**
A popular, certified, wrong definition still ranks first. The wiki says so on every page,
and the query side says so in every answer to a conflicting term.

## Files

| | |
|---|---|
| `src/common.py` | slugs, node ids, term normalisation, DAX stripping, the node/edge emitter |
| `src/fabric_api.py` | the REST calls, wrapped thin over duckrun |
| `src/harvest.py` | Fabric -> `raw/` |
| `src/parse_model.py` | TMSL -> tables, measures, terms, DAX references, source bindings |
| `src/parse_report.py` | PBIR and PBIR-Legacy -> pages, visuals, field references |
| `src/parse_code.py` | notebooks and pipelines -> reads, feeds, runs |
| `src/parse.py` | orchestrates the parsers, the scanner, the audit log, external stubs, profiles |
| `src/profiling.py` | Delta logs and duckrun value scans -> `raw/*/profiles/` |
| `src/publish.py` | the context into a lakehouse, through duckrun, and `context.json` |
| `src/deploy.py`, `src/notebook.py` | the nightly refresh: ship the code, build the notebook, schedule it |
| `src/files.py` | the working files <-> the lakehouse `Files/` section, as a diff |
| `src/files.py` | the working files <-> the lakehouse `Files/` section, as a diff |
| `src/schema.sql`, `src/graph.py` | the database, the terms, the ranking, the lineage walk, the read-back |
| `src/wiki.py` | the markdown projection |
| `src/viz.py`, `src/graph_template.html` | the standalone `graph.html` |
| `src/queries.sql` | verification and demo SQL |
| `src/selftest.py` | the whole pipeline on a synthetic tenant |
| `context.json` | which lakehouse the context went into; the only thing besides the tables that both sides touch |
| `ask/context.py` | opening the published tables, and read-only queries over them |
| `ask/fabric.py` | live DAX and live column values |
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
