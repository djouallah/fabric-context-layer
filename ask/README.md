# ask/ - the query side

`src/` harvests Fabric and publishes the context into a Fabric lakehouse of its own: the
ranked graph as one Delta table per published table under `Tables/dbo/`, and everything it
was built from and rendered into - `raw/`, `build/`, `wiki/`, `graph.html` - under `Files/`.
This folder is the other side. It reads `Tables/` only: it opens them read-only through
duckrun, answers questions from them, and runs DAX against a semantic model when a question
needs a number. Two dependencies, duckrun and DAX. It imports nothing from `src/`; if you
deleted `src/` and kept the lakehouse, everything here would still run.

It finds the lakehouse in `context.json`, which the harvest side writes when it publishes.
That file is an address, not content - workspace, lakehouse, their GUIDs, and when the
publish happened. `--db` overrides it with a `<workspace>/<lakehouse>.Lakehouse`, a
`<ws-guid>/<lh-guid>`, an abfss:// URL, a local folder of Delta tables, or a `.duckdb` file.

Reading OneLake costs about ninety seconds to open the Delta logs and ten a table after
that, so the published tables are pulled once into a file named after the publish that
produced them, under `%LOCALAPPDATA%\fabric-context` (`FABRIC_CONTEXT_CACHE` moves it).
The next command opens that in milliseconds; the next publish makes a new name, so a stale
copy cannot be read. `--refresh` re-pulls, `--no-cache` keeps none. Nothing lands in the
repo. `contract` prints which copy it used.

```
python -m ask contract                       what the lakehouse publishes, and what is missing
python -m ask scope                          build time, workspaces, models, stores, counts
python -m ask search "average price"         ranked hits: terms, measures, tables, columns
python -m ask define "average price"         every definition, ranked, with the DAX
python -m ask model <name|id> --table <t>    tables, columns, values, relationships, measures
python -m ask table <store.schema.table>     columns and profile, who writes and reads it
python -m ask lineage <node|name> [--down]   what feeds it, or what depends on it
python -m ask usage <term|node>              reports, references, mentions, downstream
python -m ask values <model> <table> <col>   distinct values and range, harvested or live
python -m ask dax <model> "EVALUATE ..."     run DAX on the model (Power BI executeQueries)
python -m ask sql "select ... from terms"    DuckDB SQL over the context tables themselves
python -m ask evals generate | run | report  the benchmark
```

`--json`, `--db`, `--refresh` and `--no-cache` are global and go BEFORE the subcommand:
`python -m ask --json define "average price"`. Exit codes: 0 ok, 2 not found or ambiguous,
3 refused (not a read-only query), 4 Fabric said no.

## The contract

The harvest side publishes tables; this side queries them. `python -m ask contract`
checks the list below against the file, so a change on either side shows up as a missing
table or column rather than a wrong answer.

| Published by `src/graph.py` | Read here for | Required |
|---|---|---|
| `nodes` (id, kind, name, workspace, item_id, parent_id, description, endorsement, owner, modified_at, attrs) | everything: models, tables, columns (`attrs.data_type`, `source_column`, `is_hidden`, `profile`), measures (`attrs.expression`), workspaces (`item_id` is the workspace GUID), external stubs (`attrs.external`) | yes |
| `edges` (src, dst, rel, weight, attrs) | `contains`, `relates_to` (`attrs.from_column`, `to_column`, `is_active`), `sources_from` (`attrs.source_workspace_id`), `references`, `reads`, `feeds`, `uses`, `mentions` | yes |
| `terms`, `definitions` | which definition to trust: rank, score, the four signals, `conflicting`, the DAX, `owner_item_id` (the dataset id `dax` executes against) | yes |
| `flow` view | the lineage walk; the recursive query lives in `context.py` | yes |
| `measure_usage`, `item_views` | which reports use a definition, how often they were opened | yes |
| `aliases` | lookup by any spelling of a term | optional |
| `item_usage` | opens, runs, queries and refreshes per item over 28 days | optional |
| `meta` | `built_at`, `schema_version`, the activity window, the workspaces | optional |
| `attrs.profile` on column nodes; `attrs.columns`, `stats`, `values`, `n_rows` on lakehouse tables | filter literals and date ranges without a live call (from `python src/run.py profile`) | optional |

When an optional part is missing the answer degrades rather than fails: no `aliases`
means name-only search, no profile means `values` asks the model live, no `meta` means
the file's modification time stands in for `built_at`.

## How a question flows

1. `scope`: what is harvested, when it was built, which models can be executed.
2. `search "<words>"`: candidates across terms, aliases, measures, tables, columns. Below
   the threshold (0.55) the honest answer is "not in the harvested context".
3. `define "<term>"`: the ranked definitions. The answer quotes rank 1, names its model,
   and says whether the others disagree.
4. `model <id> --table <t>`: the schema pack, with `profile.values` on columns the harvest
   profiled.
5. `values <model> <table> <column>`: live DAX (`TOPN(50, VALUES(...))`, `MIN`, `MAX`)
   when the harvest carries no profile. This is the one place the query side fetches
   something the harvest could have published; `python src/run.py profile` closes it.
6. `dax <model> "<EVALUATE ...>"`: executeQueries, calling the ranked measure by name so
   the governed logic runs, never a re-derivation of it.
7. The answer: value, unit, period, then measure, model, rank, score, conflict note, the
   query that ran, and freshness.

`.claude/skills/fabric-context/SKILL.md` gives Claude Code this protocol.

## What it never does

- Write to the context, or read `raw/` or `build/`.
- Read the data any way but `EVALUATE`/`DEFINE` DAX; `sql` only sees the context tables.
- Pick between conflicting definitions silently.

## The benchmark

`python -m ask evals generate` builds `ask/evals/questions.yaml` from the database alone:
definition, conflict, lineage, usage and data questions for the top terms, plus one
out-of-scope question, so it runs on any tenant without a hand-written question. Correct
what it got wrong, then `python -m ask evals run --dry-run` validates every golden DAX and
`python -m ask evals run` runs each question through Claude Code headless twice: a
baseline that sees the raw model definitions and can run DAX but has no context layer,
and a treatment with the skill. Grading is deterministic (numbers within tolerance of the
golden DAX, names that must appear, a refusal for the out-of-scope question); results land
in `ask/evals/results/`.

## Live calls

`dax` needs a Power BI token (`az login` covers it through duckrun), the tenant setting
"Dataset Execute Queries REST API", and Build permission on the model. A cold Direct Lake
model takes tens of seconds on the first query. Rows are capped (100 by default, 10,000
hard).

Opening the context itself is the other live call, and the expensive one: measured against
a real lakehouse, `duckrun.connect` took 90 s to open eleven Delta logs and every table
read another 10 s - three minutes to fetch eight megabytes, because the cost is round
trips, not bytes. Hence the downloaded copy described at the top; the first command after
a publish pays it once, the rest take milliseconds.
