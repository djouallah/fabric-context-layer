---
name: fabric-context
description: Answer questions about the harvested Microsoft Fabric tenant - business terms and their competing definitions, semantic models, tables, lineage, usage, and numbers computed from the models - using the context layer through `python -m ask`. Use for any question about a measure, a metric, a table, a report, a notebook, what feeds what, or "what is/was <metric> for <filter>".
---

# Answering from the Fabric context layer

The harvest side (`fabcontext`) has already read the tenant, ranked every competing
definition, and published the result into a Fabric lakehouse of its own; `--db` says which.
You have exactly two commands:

```
python -m ask context                  # fetch Files/context.md and print its path
python -m ask dax <ws-guid>/<model-guid> "EVALUATE ..."
```

`context.md` is the whole graph as one markdown file. It is the metadata - every term, its
ranked definitions and their DAX, every model with the two ids a query executes against,
column values, lineage, who writes what. **You read it.** `dax` is the only other thing you
run, and the only source of a number. There is no SQL, no search command and no metadata
query: if the file does not say it, it is not known.

Everything you cite comes from the file or from the query you ran, never from memory.

## Where it runs

Python 3.12 and a clone of this repo, with the commands run from the clone's root: `ask/` is
not an installed package. If the working directory is not the clone, ask once where it is.
There, `pip install -e .` once. `az login` is where the Power BI token comes from; with no
login available, say so and stop - do not set up credentials. `--db` is global and goes
before the subcommand: when `context` exits 2 with "nothing published yet", ask for the
`abfss://` URL `fabcontext.harvest()` returned or `<ws-guid>/<lakehouse-guid>` and pass it to
`context`; `dax` takes its ids from the file and needs none. Never search the machine for
it, and never run `python -m fabcontext` to make one.

## Protocol

1. **Fetch it, once per conversation**: `python -m ask context`. It prints a path; read that
   file. On a big tenant read the header and "How to use this file" first, then grep for the
   section you need - `### term:`, `### model:`, `### report:`, `### store:`. If the command
   says nothing is published, ask for `--db` (see Where it runs) and stop; do not run the
   harvest side.

2. **Find the term.** Terms are indexed in the `## Terms` table at the top and detailed as
   `### term: <term_id>` below. Check the `also known as` line - a term merges its
   spellings. If no section covers the question's nouns, say the harvested context has
   nothing for it, name the nearest sections, and stop. Do not guess.

3. **Take rank 1.** The ranked table under a term is the answer, top row. The four signals
   have already weighed authority, popularity, relevance and freshness; that is what the
   layer is for. Take rank 1 and go on - unless the question names a model or workspace, in
   which case take that one.

   A conflict does not change the pick, only the disclosure - and the disclosure goes in
   Sources (step 6), never beside the number. When the term says `conflicting: yes`, one
   line there: which measure you used, which model it lives in, its rank out of how many,
   and that the others differ. Do not lay the competing expressions out, do not invite the
   reader to choose, and never end by saying the number would be different under another
   definition.

4. **Write DAX that calls that measure by name**, never re-derives its logic:

   ```
   EVALUATE ROW("v", CALCULATE([<measure>], '<table>'[<column>] = "<value>"))
   EVALUATE TOPN(20, SUMMARIZECOLUMNS('<table>'[<column>], "v", [<measure>]), [v], DESC)
   ```

   One `EVALUATE` per call. Keep result sets small (`TOPN`, `--max-rows`). Filter literals
   come from the `values:` on a column in the model's section. When a column has none, one
   more DAX call fetches them: `EVALUATE TOPN(50, VALUES('<table>'[<column>]))`. **Never
   invent a literal.**

5. **Run it**: copy the `run:` line from the model's section for the ids, then
   `python -m ask dax <ws-guid>/<model-guid> "<query>"`. On an error, fix the query and
   retry at most twice, then report the error text verbatim.

6. **Answer in three blocks, in this order.** Nothing from a later block may appear in an
   earlier one.

   **The number** comes first and alone: its value, unit and period, then stop. Round it to
   what a person reads - 1.37 billion MWh, not 1,374,388,307.9978 - and keep the exact figure
   for Sources. If the question asked for a breakdown, this block is the table. No measure
   name, no model, no id, no rank, no caveat here; the reader must be able to stop after this
   block and have the answer.

   **Sources** comes second, under that heading: the measure and its expression, the model
   and its id, the workspace, the rank out of how many and the score, the query that ran, the
   row count, the unrounded value if you rounded, and freshness (`built_at` from the file's
   header, and the model's own latest-date measure if it has one). The one-line conflict note
   lives here too, never beside the number.

   **Confidence** comes last, on one line: high, medium or low, with the reason in one clause
   beside it - which of the layer's own numbers says so, not a feeling, not a paragraph, and
   never an argument with the answer you just gave.

   | | when |
   |---|---|
   | high | one definition, or rank 1 clear of rank 2 by more than about 1.0 of score; endorsed or documented; the owning model is used and fresh |
   | medium | conflicting but rank 1 leads clearly; or the owning model has little recent usage; or the context was built a while ago |
   | low | rank 1 and rank 2 within about 0.5 of each other; or the section you matched is a loose fit for the question; or the rank-1 model has no endorsement and no usage at all |

   At **low**, name the rival definition in Sources - that is the case where a second
   expression belongs in the answer. Above that, one line about the conflict is enough.

   Then stop. You may offer one follow-up, but it may not carry a number: a figure nobody
   asked for, handed over without sources, is an unsourced answer.

   Sources and confidence are provenance for an answer already given, not a hedge around it.
   Never withhold the number in order to discuss the definitions.

7. **When no measure covers it, that is the answer.** A term with no definition, or a table
   only listed under its store, has nothing in this tenant that agrees what its number
   means. Say that plainly, then give what the file does know: the columns and their
   profiled values, who writes the table, what reads it, which tier it is in. Do not compute
   it another way - there is no other way here, and inventing one would invent the
   definition this layer exists to find.

   Tier is the same signal: **1** feeds a semantic model, **2** is read or written by a
   notebook or pipeline, **3** is harvested and nothing in these workspaces refers to it. A
   tier-3 table is a describe-only answer by definition.

## Metadata questions

All of these are read out of `context.md`; none of them run anything.

- *Where is X defined, which should I trust?* - `### term: <x>`, top row of its table.
- *Do the definitions of X agree?* - the `conflicting:` field; answer yes or no first, then
  the expressions from the `#### <rank>.` blocks.
- *What feeds X?* - the `upstream:` line on the term, and the `### notebook:` /
  `### pipeline:` sections that write the tables it names.
- *Which reports use X?* - the `used in reports:` line on the term, and `fields used:` on
  each `### report:`.
- *What is in table T?* - `#### table:` under its `### store:`. A table named only in a
  store's `not referenced` list was harvested and nothing reads it.

## Never

- Never re-derive a number a measure already defines. A measure exists -> call it by name.
- Never compute a number for something no measure covers. Say so instead.
- Never run the harvest side (`python -m fabcontext ...`), never write files, never read the
  harvested JSON in `Files/raw` - reading it is exactly what this layer replaces.
  `context.md` is the rendered context, not that JSON; reading it is the point.
- Never answer a number without having run it.
- Never refuse to pick between definitions. The ranking is the layer's job; hand back rank
  1, sourced, with a confidence. "Here are three definitions, you choose" is a non-answer.
- Never pick silently either: the one-line conflict note and the confidence are the
  disclosure, and at low confidence the rival definition is named.
