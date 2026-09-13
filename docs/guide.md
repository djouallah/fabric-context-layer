# Running fabric-context-layer

The operational side. The [README](../README.md) has the idea.

The harvest is a pip package, `fabcontext`. It runs inside a Fabric notebook, reads the
tenant, and leaves a lakehouse behind. The URL it returns is the only thing that crosses the
boundary: no config file, no shared state, nothing kept on the machine that ran it.

The context is not another item. There is one per tenant, ranked per domain, built by the
platform and hidden; an agent never needs its address. This POC keeps it in a lakehouse
because that is the durable store it can write to, and the returned URL stands in for
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
re-derived. Weights are hand-picked, in `fabcontext/graph.py`. **Rank is not correctness** - a
popular, certified, wrong definition still wins, and every answer to a conflicting term
says so in one line.

## Run it

Two lines in a Fabric notebook:

```python
%pip install fabcontext
```

```python
import fabcontext
url = fabcontext.harvest("My Workspace")
```

That is the whole interface. The first call creates a lakehouse called `context_layer` in
that workspace; every later call updates it. It returns the lakehouse's Tables root, which is
what the query side takes as `--db`.

Several workspaces go in one call, as a list of names or GUIDs, with `to` saying where the
context lands (otherwise it is the first workspace named):

```python
url = fabcontext.harvest(["sqlengines", "Sales", "Finance"], to="sqlengines/context_layer")
```

Name them together rather than calling `harvest` once each. The Scanner API takes up to a
hundred workspaces per call and is the only source of endorsement and cross-workspace
lineage, so a model in Sales reading a lakehouse in Finance resolves to a real node only when
both are in the same scan; harvested separately, that edge is an `external` stub instead.

A lakehouse accumulates. A run pulls the previous `Files/raw` down and the parse reads every
workspace folder it finds there, so harvesting A and later B into the same lakehouse gives a
context holding both. That is usually what you want - but `scanner.json` is cached for
`stale_after_days` and covers only the workspace set of the run that fetched it, so pass
`refresh=True` the once when you add a workspace, and let the incremental behaviour resume
afterwards.

**The install replaces nothing and needs no kernel restart.** `fabcontext` declares five
dependencies and the Fabric Python 3.12 runtime already has all five at or above the
required version (`docs/fabric-runtime.txt`), so pip fetches one pure-Python wheel and
leaves duckdb and deltalake - both native - exactly as they are. That is the reason for the
version floors in `pyproject.toml`; raising one past the runtime's version would cost every
user a restart.

The knobs, all optional:

| | |
|---|---|
| `to="<workspace>/<lakehouse>"` | where to publish. Default: `context_layer` in the first workspace named |
| `days=28` | days of activity events. The audit log keeps 30 |
| `query_log=True` | also read the monitoring Eventhouse (below) |
| `refresh=True` | refetch everything rather than only what changed |
| `profile=False` | skip lakehouse columns, stats and values |
| `values=False` | profile from the Delta log only, without the distinct-value scan |
| `wiki=False` | skip the markdown wiki, `context.md` and `graph.html` |
| `aliases={"revenue": ["Net Sales"]}` | merges the word lists cannot make |
| `folder="context"` | workspace folder to put the lakehouse in |

From a terminal, `python -m fabcontext "My Workspace"` takes the same options as flags.

**A re-run is cheap.** The lakehouse keeps the previous harvest under `Files/raw`, and a run
against an existing one pulls it down first: a definition is refetched only when its
`lastUpdatedDate` has moved, and the audit log is one file per UTC day with today and
yesterday refetched. The scanner result and the store table lists carry no freshness signal
of their own, so they refetch once older than `stale_after_days` (default 1) - without that
they would freeze on the first run and never move again.

`query_log=True` additionally reads each workspace's **workspace monitoring** Eventhouse for
the DAX that actually ran, and attributes it to the measures each query called - the one
signal that says a measure was *evaluated* rather than merely written into a report. It is
off by default: monitoring bills against the capacity, and a workspace without it is
skipped, so partial coverage adds to the popularity signal rather than replacing it. Query
text stays in `raw/`; only counts are published.

The lakehouse is created schema-enabled if it does not exist. The context then lives in the
tenant it describes: its SQL analytics endpoint answers T-SQL over `dbo.nodes`, `dbo.edges`,
`dbo.terms`, `dbo.definitions` and the rest, Power BI and notebooks can read it, and a
Fabric data agent can be pointed at it.

```sql
-- on the lakehouse's SQL analytics endpoint
SELECT term_id, label, n_definitions FROM dbo.terms WHERE conflicting = 1;
SELECT name, owner_item_name, rank, expression
  FROM dbo.definitions WHERE term_id = 'avg-price' ORDER BY rank;
```

## Ask it

`--db` is the URL `harvest()` returned; it identifies the lakehouse and is the only thing the
two halves share. The client is two commands:

```powershell
python -m ask --db <url> context              # fetch Files/context.md, the whole graph as one file
python -m ask dax <ws-guid>/<model-guid> "EVALUATE ROW(\"v\", [<measure>])"
```

`context.md` is the metadata: every term with its ranked definitions and their DAX, every
model with the two ids a query executes against, column values, lineage, reports, and the
long tail named under its store. An agent reads it. One round trip fetches it, it opens no
Delta table, and the copy lands in `%LOCALAPPDATA%\fabric-context`; `--refresh` re-fetches.

`dax` is the only other call and the only source of a number. It takes the ids straight from
the file - every model section prints a `run:` line to copy - so running a number looks
nothing up. `--json` and `--db` are global flags and go before the subcommand.

**There is no SQL here.** A number comes from DAX calling a ranked measure by name, or it
does not come. A table no semantic model covers has nothing in the tenant that agrees what
its number means, so the honest answer is to say that and describe what is known, not to
compute one and imply a definition nobody wrote. That is also why the client needs no
database driver at all: no duckdb, no deltalake, no DuckDB extension, no pull.

**This is the clone-side path, and it is not the only one.** `python -m ask` needs Python
3.12, this clone, `pip install -e .` and `az login`, and in exchange it reads the whole graph
- lineage, reports, tables, profiled column values. The repo ships that protocol as a Claude
Code skill (`.claude/skills/fabric-context/SKILL.md`); GitHub Copilot loads it from
`.claude/skills/` too, and `.github/copilot-instructions.md` is the rule that sends tenant
questions to it.

Anyone who only wants a number installs nothing. The harvest also publishes the ranking as a
semantic model (`context_model`), so one DAX query says which definition wins and carries the
ids to run it against, and a second runs it - over a read-only Power BI connection and no
clone at all. That is [agent/](../agent/), one folder per tool: Claude, GitHub Copilot, Scout,
Microsoft 365 Copilot. The trade is what the model exposes: `terms`, `definitions` and
`aliases` are the ranking, so that side answers *what is X* and *which definition wins*, and
says plainly that lineage, reports and table detail are out of its reach.

`wiki/` still opens in Obsidian, and holds the same content as linked pages.

How a question flows:

1. `context` fetches the file; its `## Terms` index and `### term:` sections find the term
   by any of its spellings.
2. The ranked table under that term gives every definition with the four signals, the DAX,
   and the model that owns it. A conflicting term says so.
3. The model's section gives the tables, columns, relationships and source tables; profiled
   columns carry their distinct values and ranges, so "NSW" becomes
   `'<table>'[<column>] = "NSW"` from the data, not from memory.
4. `dax` runs a query that calls the ranked measure by name through the Power BI
   executeQueries API, so the governed logic runs rather than a re-derivation of it.
5. The answer cites the measure, model, rank, score, conflict note and the query that ran.

`python -m ask evals generate` derives a benchmark from the graph (definition, conflict,
lineage, usage and data questions for the top terms, plus one out-of-scope question). It is
a maintainer tool and the one thing that still opens the published tables, through
`ask/db.py`;
`python -m ask evals run` runs it through Claude Code headless with and without the
context layer and reports accuracy per class. Nothing in it names a tenant.

## On a schedule

There is no deployer here. Put the two cells in a notebook and schedule that notebook from
Fabric, which is the tool for it - and then the laptop is out of the loop entirely. Inside a
notebook the run needs no sign-in: the notebook's own identity supplies every token.

A scheduled run harvests incrementally for the reasons above, and publishes over the same
lakehouse. If a run dies between the publish and the file push, `Tables/` is newer than
`Files/` until the next run; harmless.

## What it needs

| | |
|---|---|
| Python | 3.12, the Fabric notebook default. `pip install fabcontext` |
| Auth | Fabric admin, for the Scanner API and the audit log. A workspace member gets definitions and inventory but no endorsement, no lineage across workspaces and no usage. Creating the lakehouse needs contributor on the target workspace. `ask dax` needs Build permission on the model and the tenant setting *Dataset Execute Queries REST API*. |
| Tenant settings | *Enhance admin API responses with detailed metadata* and *Enhance admin API responses with DAX and mashup expressions*. Without the second, the scanner returns measures with no expression - the harvest warns you. |
| Workspace monitoring | only for `query_log=True`; enabled per workspace, bills against the capacity |

Five dependencies, every one already in the Fabric runtime: `duckdb` and `deltalake` do the
engine and the Delta I/O, `requests` the REST calls, `azure-identity` the sign-in outside a
notebook, and `azure-storage-file-datalake` the loose files - OneLake speaks ADLS Gen2, so
that is the stock SDK pointed at `onelake.dfs.fabric.microsoft.com`.

Two choices worth knowing about, both made to keep that list short. Delta tables are read
through delta-rs's own DataFusion engine (`QueryBuilder`) rather than DuckDB's `delta_scan`,
because the `delta` and `azure` DuckDB extensions are not bundled and would be downloaded in
every notebook session. And a table is published by streaming a DuckDB relation straight
into `write_deltalake` over an Arrow C stream, so nothing is materialised in between and
pyarrow is never needed.

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
owner, modified_at, attrs, tier)` and `edges(src, dst, rel, weight, attrs)`.

Node kinds: `workspace, semantic_model, model_table, column, measure, report, page, visual,
report_measure, dashboard, lakehouse, warehouse, lakehouse_table, notebook, pipeline,
dataflow, datasource, user, term, other_item, unresolved`. A lakehouse something binds to
but nobody harvested is a `lakehouse` node with `attrs.external = true`.

Relations: `contains, defines, references, sources_from, reads, feeds, runs, refreshes,
uses, relates_to, mentions, viewed_by, depends_on`.

A reference that cannot be bound to a harvested item still gets an edge, pointed at an
`unresolved:` id that deliberately has no node, so the miss is counted rather than dropped.
The tests assert the count is zero for everything else.

Derived on top: `terms`, `definitions`, `aliases`, `item_usage` (with `item_views` kept as
a view), `query_usage`, `query_stats`, `meta`, and the views `flow` and `measure_usage`.

### The tier

`nodes.tier` says how close a node sits to something anyone agreed on. It is derived by
`fabcontext/graph.py`'s `_tier`, not parsed, and everything starts at 1 and is demoted from there:

| tier | means | who is in it |
|---|---|---|
| 1 | load-bearing | a term, a measure, a model, a report, a notebook, a table a semantic model sources from |
| 2 | reachable | a table a notebook or pipeline reads or writes, but no model is built on |
| 3 | inventory | harvested, and nothing in the harvested workspaces refers to it |

Below tier 3 there is one thing that is not kept at all. Fabric creates a semantic model
beside every lakehouse by itself; it carries whatever tables got synced into it and defines
no measure, so it can answer nothing. Worse, its tables `sources_from` the lakehouse tables
underneath, and that edge is what marks a table tier 1 - one auto-synced default model
promotes a whole sandbox lakehouse to load-bearing and the tier stops meaning anything. So
`graph.py` deletes them outright, with their tables, columns, edges and monitoring rows,
before anything is derived. The exception is one a report or a pipeline actually points at:
a person built on that, so it stays, demoted to tier 3, rather than breaking their lineage.

Tier 3 is most of a real tenant: on the three workspaces here it is 685 of 750 lakehouse
tables and every SQL endpoint. The harvest still collects all of it - a lakehouse table list is one call per
store, and an inventory that stops at what a model happens to use cannot say what is
unused, where a table came from, or that a name exists at all - but the renderers hold it
back:

- **the wiki** gives it no page of its own. It is named on its store's page under
  *Not referenced*, and on its workspace page. This is what takes the wiki from 931 item
  pages to 228.
- **`context.md`** details tier 1 and 2 and names tier 3 in one list under its store, with
  no columns and no section of its own.
- **`graph.html`** counts it and hides it behind the *Unreferenced* toggle.
Nothing is dropped: every tier is in `Tables/`, and a tier-3 table is still named, still
attributed to its store, still walkable in lineage. What it does not have is a number. No
semantic model covers it, so nothing in the tenant agrees what its number would mean, and
the client will say that rather than compute one.

## Status

`pytest` builds a synthetic tenant covering both report formats, a Direct Lake model, a
DirectQuery model, a model bound to an unharvested store, a notebook, a pipeline, a profile
file, an audit log and a query log, runs the whole pipeline over it and checks the result -
including the publish, the read-back against the published column contract, and the file
push, with a temp folder standing in for the lakehouse. No Fabric access needed; run it
after touching any parser.

```powershell
python -m venv .venv
.venv\Scripts\pip install -e . -r requirements-dev.txt
.venv\Scripts\pytest
```

`requirements-dev.txt` pins duckdb and deltalake to the versions Fabric ships. That is not
tidiness: a laptop otherwise resolves newer ones, and a suite green against those says
nothing about the runtime this targets. One test asserts that importing `fabcontext` pulls
in no duckrun, dbt, pyarrow or obstore, and another that every declared dependency appears
in `docs/fabric-runtime.txt`.

The harvest has run against three real workspaces. Every REST call in `fabcontext/api.py`
was written from the documented contract, and none of them needed correcting. Failures are
caught per item and recorded in `raw/<workspace>/manifest.json` rather than stopping the run.

**`parse_report.py` has one real report of coverage.** Reports carry the popularity signal;
on a report-free workspace definitions are ordered on authority, model usage and freshness.

## Files

| | |
|---|---|
| `fabcontext/__init__.py` | `harvest()` - the one call, and the step timing table |
| `fabcontext/_fabric/` | everything that talks to Fabric: tokens, REST, the workspace handle, OneLake files, Delta I/O, the TMSL patterns |
| `fabcontext/common.py` | slugs, node ids, term normalisation, DAX stripping, the node/edge emitter |
| `fabcontext/api.py` | the REST calls, one per thing the harvest wants to know |
| `fabcontext/fetch.py` | Fabric -> `raw/`, the query log included |
| `fabcontext/parse_model.py` | TMSL -> tables, measures, terms, DAX references, source bindings |
| `fabcontext/parse_report.py` | PBIR and PBIR-Legacy -> pages, visuals, field references |
| `fabcontext/parse_code.py` | notebooks and pipelines -> reads, feeds, runs |
| `fabcontext/parse.py` | orchestrates the parsers, the scanner, the audit log, the query log, external stubs, profiles |
| `fabcontext/profiling.py` | Delta logs and DataFusion value scans -> `raw/*/profiles/` |
| `fabcontext/publish.py` | the context into a lakehouse, as Delta |
| `fabcontext/files.py` | the working files <-> the lakehouse `Files/` section, as a diff |
| `fabcontext/schema.sql`, `fabcontext/graph.py` | the database, the terms, the ranking, the lineage walk, the read-back |
| `fabcontext/wiki.py` | the markdown projection: `wiki/` as linked pages, `context.md` as one file |
| `fabcontext/viz.py`, `fabcontext/graph_template.html` | the standalone `graph.html` |
| `tests/` | the whole pipeline on a synthetic tenant |
| `docs/fabric-runtime.txt` | what the Fabric Python 3.12 runtime ships; the dependency list rests on it |
| `ask/context.py` | the client: find the lakehouse, fetch `context.md`. No database driver |
| `ask/fabric.py` | the only network call that returns a number: DAX on a model |
| `ask/db.py` | the published tables in DuckDB - read by the benchmark, and by nothing else |
| `ask/__main__.py` | `python -m ask` |
| `ask/evals.py` | the generated benchmark and its runner |
| `.claude/skills/fabric-context/SKILL.md` | in this clone: how Claude Code uses `python -m ask` |
| `.github/copilot-instructions.md` | in this clone: the rule that sends tenant questions to that skill |
| `agent/SKILL.md` | no clone: the protocol over Power BI alone, installed by Claude, Copilot and Scout alike |
| `agent/<tool>/README.md` | no clone: where each tool wants that file, and how it signs in |
| `fabcontext/semantic_model.py` | the context's own Direct Lake model - the ranking, queryable as DAX |
| `agent/m365/instructions.md` | the M365 Copilot agent's instructions - see [agent/m365/](../agent/m365/) |

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
  word and stay apart until the `aliases=` argument says otherwise. Sorting the words loses
  order: a term page lists every alias so a wrong merge is visible.
- **A model bound through a SQL endpoint outside the harvest** is a stub with the endpoint
  id only; its lakehouse and workspace stay unknown until that workspace is harvested.
- Only the workspaces you harvest are in the graph. A reference to anything outside them
  shows as unresolved or external.
