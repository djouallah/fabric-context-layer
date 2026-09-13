# ask/ - the query side

`fabcontext` harvests Fabric and publishes the context into a lakehouse of its own: the
ranked graph as one Delta table per published table under `Tables/dbo/`, and everything it
was built from and rendered into - `raw/`, `build/`, `wiki/`, `context.md`, `graph.html` -
under `Files/`. This folder is the other side, and it is deliberately small.

**Two commands.**

```
python -m ask --db <url> context             fetch Files/context.md and print its path
python -m ask dax <ws-guid>/<model-guid> "EVALUATE ..."
```

`context.md` is the whole graph as one markdown file: every term with its ranked
definitions and their DAX, every model with the two ids a DAX query executes against,
column values, lineage, reports, and the long tail named under its store. It is the
metadata, and it is read, not queried. One round trip fetches it - against the twelve Delta
logs the old query side opened first - and the copy lands under `%LOCALAPPDATA%\fabric-context`
(`FABRIC_CONTEXT_CACHE` moves it); `--refresh` re-fetches.

`dax` is the only other call and the only source of a number. Every model section in the
file prints a `run:` line carrying its `<workspace-guid>/<model-guid>`, so a number is one
copy away and nothing is looked up. **The client opens no database**: no duckdb, no
deltalake, no DuckDB extension. `tests/test_client_is_thin.py` asserts it.

**There is no SQL.** A number comes from DAX calling a ranked measure by name, or it does
not come. A table no semantic model covers has nothing in the tenant that agrees what its
number means, so the answer is to say that and describe what the file knows - not to
compute one and imply a definition nobody wrote.

`--json`, `--db` and `--refresh` are global and go BEFORE the subcommand:
`python -m ask --json context`. Exit codes: 0 ok, 2 not found, 3 refused (not a read-only
DAX query), 4 Fabric said no.

`ask/db.py` still pulls the published tables into DuckDB, for one caller: the benchmark
below, which builds its questions out of the graph. Nothing a user asks goes through it.

## The contract

The harvest side publishes tables and renders `context.md` from them. The client reads the
file; `ask/db.py` and `tests/test_publish.py` carry the column list below, so a change on
either side shows up as a missing table or column rather than a wrong answer.

| Published by `fabcontext` | Read here for | Required |
|---|---|---|
| `nodes` (id, kind, name, workspace, item_id, parent_id, description, endorsement, owner, modified_at, attrs) | everything: models, tables, columns (`attrs.data_type`, `source_column`, `is_hidden`, `profile`), measures (`attrs.expression`), workspaces (`item_id` is the workspace GUID), external stubs (`attrs.external`) | yes |
| `edges` (src, dst, rel, weight, attrs) | `contains`, `relates_to` (`attrs.from_column`, `to_column`, `is_active`), `sources_from` (`attrs.source_workspace_id`), `references`, `reads`, `feeds`, `uses`, `mentions` | yes |
| `terms`, `definitions` | which definition to trust: rank, score, the four signals, `conflicting`, the DAX, `owner_item_id` (the dataset id `dax` executes against) | yes |
| `flow` view | the lineage walk; the recursive query lives in `context.py` | yes |
| `measure_usage`, `item_views` | which reports use a definition, how often they were opened | yes |
| `aliases` | lookup by any spelling of a term | optional |
| `item_usage` | opens, runs, queries and refreshes per item over 28 days | optional |
| `meta` | `built_at`, `schema_version`, the activity window, the workspaces | optional |
| `attrs.profile` on column nodes; `attrs.columns`, `stats`, `values`, `n_rows` on lakehouse tables | filter literals and date ranges without a live call (from the harvest's profile step) | optional |

When an optional part is missing the rendered file degrades rather than fails: no
`aliases` means a term lists no other spellings, no profile means a column carries no
`values:` line and a filter literal needs one more DAX call, no `meta` means a thinner
header.

## How a question flows

1. `context`: fetch the file, read its header and "How to use this file".
2. Find the term - the `## Terms` index, then its `### term:` section. Below no matching
   section, the honest answer is "not in the harvested context".
3. Rank 1 of that term's table is the answer. It names its model and says whether the
   others disagree.
4. That model's `### model:` section gives the tables, columns and relationships, with
   `profile.values` on columns the harvest profiled - so "NSW" comes from the data.
5. `dax <ws>/<model> "<EVALUATE ...>"`: executeQueries, calling the ranked measure by name
   so the governed logic runs, never a re-derivation of it.
6. The answer: value, unit, period, then measure, model, rank, score, conflict note, the
   query that ran, and freshness.
7. No measure covers it: say so, give the columns, who writes the table and what reads it,
   and stop.

`.claude/skills/fabric-context/SKILL.md` gives Claude Code this protocol.

## What it never does

- Write to the context, or read `raw/` or `build/`.
- Read the data any way but `EVALUATE`/`DEFINE` DAX.
- Compute a number for something no measure defines.
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

`dax` needs a Power BI token (`az login` covers it), the tenant setting
"Dataset Execute Queries REST API", and Build permission on the model. A cold Direct Lake
model takes tens of seconds on the first query. Rows are capped (100 by default, 10,000
hard).

Fetching `context.md` is the other live call, and it is one request. The cost it replaces
is why: measured against a real lakehouse, opening eleven Delta logs took 90 s and every
table read another 10 s - three minutes to fetch eight megabytes, because the cost is round
trips, not bytes. The benchmark still pays that, through `ask/db.py`; the client no longer
does.
