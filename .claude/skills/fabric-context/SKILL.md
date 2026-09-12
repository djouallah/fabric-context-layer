---
name: fabric-context
description: Answer questions about the harvested Microsoft Fabric tenant - business terms and their competing definitions, semantic models, tables, lineage, usage, and numbers computed from the models - using the context layer through `python -m ask`. Use for any question about a measure, a metric, a table, a report, a notebook, what feeds what, or "what is/was <metric> for <filter>".
---

# Answering from the Fabric context layer

The harvest side (`src/`) has already read the tenant and published the context into a
Fabric lakehouse of its own; `context.json` says which. You answer from that through
`python -m ask`, which reads it with duckrun, and run DAX against a semantic model when a
question needs a number. Everything you cite comes from the tool output, never from memory.

`--json` is a global flag and goes BEFORE the subcommand: `python -m ask --json define
"<term>"`. The first command after a publish downloads the context and takes a couple of
minutes; every one after that is instant. If a command says "nothing published yet", say
so and stop - do not run the harvest side yourself.

## Protocol

0. **Scope, once per conversation**: `python -m ask scope`. Note `built_at`, the workspaces,
   and the models (their `item_id` and `workspace_id` are what `dax` executes against).
   Empty auto-created models are counted, not listed; each store shows `n_used` (tables
   something actually reads) beside `n_tables` (tables harvested).

1. **Search** the question's nouns: `python -m ask search "<words>"`. Terms come first,
   then measures, tables, columns. If the best score is under 0.55, answer that the
   harvested context has nothing for it, list the nearest hits, and stop. Do not guess.

2. **Define** the term: `python -m ask define "<term>"`. **Rank 1 is the answer.** The four
   signals have already weighed authority, popularity, relevance and freshness; that is
   what the layer is for. Take rank 1 and go on to step 3 - unless the question names a
   model or workspace, in which case take that one.

   A conflict does not change the pick, only the disclosure. When the term is
   **conflicting**, say so in **one line** with the answer - which measure you used, which
   model it lives in, its rank out of how many, and that the others differ. Do not lay the
   competing expressions out beside the answer, do not invite the reader to choose, and
   never end by saying the number would be different under another definition. If they
   want the alternatives they will ask, or run `define` themselves.

3. **Model**: `python -m ask model <model> --table <table>` for the tables the DAX
   touches and for the dimension you will filter on. Filter literals come from the
   column's `profile.values`; when a column has no profile, `python -m ask values <model>
   <table> <column>` fetches them live. Date ranges come from `profile.min/max` or
   `values`. Never invent a literal.

4. **Write DAX that calls the ranked measure by name**, never re-derives its logic:

   ```
   EVALUATE ROW("v", CALCULATE([<measure>], '<table>'[<column>] = "<value>"))
   EVALUATE TOPN(20, SUMMARIZECOLUMNS('<table>'[<column>], "v", [<measure>]), [v], DESC)
   ```

   One `EVALUATE` per call. Keep result sets small (`TOPN`, `--max-rows`).

5. **Run it**: `python -m ask dax <model> "<query>"`. On an error, fix the query and retry
   at most twice, then report the error text verbatim.

6. **Answer** with the value, unit and period **first** - one number, the one you ran.
   Then **sources**, then **confidence**.

   - **Sources**: the measure and its expression, the model and its id, the workspace, the
     rank and score, the query that ran, the row count, and freshness (`built_at` from
     `scope`, and the model's own latest-date measure if it has one).
   - **Confidence**: how sure the layer is, and which of its own numbers says so - not a
     feeling. State it as high, medium or low with the reason on the same line:

     | | when |
     |---|---|
     | high | one definition, or rank 1 clear of rank 2 by more than about 1.0 of score; endorsed or documented; the owning model is used and fresh |
     | medium | conflicting but rank 1 leads clearly; or the owning model has little recent usage; or the context was built a while ago |
     | low | rank 1 and rank 2 within about 0.5 of each other; or the `search` hit that got you here scored under 0.7; or the rank-1 model has no endorsement and no usage at all |

     At **low**, say plainly that the layer cannot separate the top two, and name the rival
     definition - that is the case where a second expression belongs in the answer. Above
     that, one line about the conflict is enough.

   Sources and confidence are provenance for an answer already given, not a hedge around
   it. Never withhold the number in order to discuss the definitions.

7. **A measure's number comes from DAX. Everything else falls back to SQL.**
   `python -m ask sql "<select ...>"` is DuckDB SQL, and it reads two things:

   - **the context tables** (nodes, edges, terms, definitions, aliases, item_usage,
     query_usage, flow) - for a metadata question the other commands do not cover;
   - **the data itself**, when the query qualifies a table with a harvested store:
     `select ... from coffee.benchmark_tests.contoso_sales`. The store is attached
     read-only over its SQL analytics endpoint and the two can be joined in one statement.

   The order matters. If `search` and `define` find a **measure** for the question, use
   DAX - that measure is the agreed definition, and re-deriving it in SQL is exactly the
   thing this layer exists to stop. Fall back to SQL only when **no model covers the
   table**: `search` returns lakehouse tables and columns but no measure, or `ask table`
   shows the table feeds no model.

   `search` prints a `tier` per hit, and it is the same signal: **1** feeds a semantic
   model, **2** is read or written by a notebook or pipeline, **3** is harvested and
   nothing in these workspaces refers to it. A tier-3 hit is a SQL answer by definition -
   no measure can cover it - so say plainly that the number has no agreed definition
   behind it. Tier never changes a score, only the order of equal ones. Say which route you used in the answer, and for a SQL
   answer say plainly that the number has no agreed definition behind it - you computed
   it from the raw table.

   The dialect is **DuckDB**, not T-SQL, even for a store: `limit`, not `top`. Get the
   schema from `python -m ask table <store.schema.table>` - it is often not `dbo`.

## Metadata questions

- *Where is X defined, which should I trust?* - steps 1 and 2.
- *Do the definitions of X agree?* - step 2; answer yes or no first, then the expressions.
- *What feeds X?* - `python -m ask lineage "<measure or term>"` (add `--down` for what
  depends on it). A `lakehouse` marked external was not harvested; say so.
- *Which reports use X?* - `python -m ask usage "<term>"`.
- *What is in table T?* - `python -m ask table <store.schema.table>`.

## Never

- Never re-derive in SQL a number a measure already defines. A measure exists -> DAX.
- Never run `python src/run.py ...` (the harvest side), never write files, and never read
  the harvested JSON directly - it is in the lakehouse's `Files/raw` and in the working
  folder, and reading it is exactly what the context layer exists to replace.
- Never answer a number without having run it.
- Never refuse to pick. The ranking is the layer's job; hand back rank 1, sourced, with a
  confidence. "Here are three definitions, you choose" is a non-answer.
- Never pick silently either: the one-line conflict note and the confidence are the
  disclosure, and at low confidence the rival definition is named.
