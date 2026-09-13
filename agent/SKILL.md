---
name: fabric-context
description: Answer a business-metric question with a single number taken from the tenant's ranked definitions - "what is revenue", "what was <metric> for <filter>", "which definition of <term> should I trust". Resolves the term against the published context model, takes the top-ranked definition, and runs that measure by name as DAX on the model that owns it.
---

# Answering from the context layer

The harvest has already read the tenant, ranked every competing definition of every business
term, and published the ranking as a semantic model called **`context_model`**. You answer by
asking that model which definition wins, then running the winning measure on the model that
owns it.

**Nothing is installed and nothing is cloned.** You need the Azure CLI signed in
(`az login`) and the context model's two ids. That is the whole setup.

## The context model's ids

`$FABRIC_CONTEXT_MODEL` holds them as `<workspace-guid>/<model-guid>`. If it is unset, ask:

> I need the context model. In Fabric, open the workspace holding the context lakehouse,
> click the semantic model named **context_model**, and paste me the address from your
> browser.

Read both ids out of the address - it contains `/groups/<workspace-guid>/datasets/<model-guid>`.
If you are given a workspace *name*, say you cannot look a name up and ask for the link.
Remember the ids for the rest of the conversation and do not ask twice.

**Never guess or construct a GUID.** If a query answers "Invalid dataset or workspace", say
the link looks wrong and ask again rather than trying variations.

## The one command

Every query - against the context model and against the model that owns a measure - is the
same call with different ids. Write the DAX to a file; do not inline it. The body is JSON and
the DAX inside it is a JSON string, so every `"` in the query becomes `\"`, and getting that
wrong inline is the easiest way here to send a query you did not mean.

```bash
cat > query.json <<'EOF'
{"queries":[{"query":"EVALUATE ROW(\"v\", [Total Revenue])"}]}
EOF

az rest --method post --resource "https://analysis.windows.net/powerbi/api" \
  --url "https://api.powerbi.com/v1.0/myorg/groups/<ws-guid>/datasets/<model-guid>/executeQueries" \
  --headers "Content-Type=application/json" --body @query.json
```

Rows come back under `results[0].tables[0].rows`, keyed by the names you gave in
`SELECTCOLUMNS` or `ROW`, in square brackets: `"[measure]"`, `"[v]"`.

An error comes back as `ERROR: Bad Request(...)` carrying a `DetailsMessage`. Read that
message - it says whether the model is unreachable, the measure does not exist, or the query
is malformed.

## Step 1 - resolve the term

Take the business noun from the question, lowercase it, and run this against the **context
model**:

```dax
EVALUATE
CALCULATETABLE(
    SELECTCOLUMNS('definitions',
        "rank", 'definitions'[rank],
        "measure", 'definitions'[name],
        "model", 'definitions'[owner_item_name],
        "workspace_id", 'definitions'[workspace_id],
        "model_id", 'definitions'[owner_item_id],
        "table", 'definitions'[table_name],
        "expression", 'definitions'[expression],
        "score", 'definitions'[score]),
    'aliases'[alias_norm] = "<term>")
```

`aliases` holds every spelling of every term, which is what lets the user's own wording land.
If that returns nothing, try the label instead:

```dax
    'terms'[label] = "<term>"
```

and if still nothing, widen once to see what the layer does know:

```dax
EVALUATE TOPN(15, SELECTCOLUMNS('terms', "term", 'terms'[label],
    "n", 'terms'[n_definitions]), 'terms'[n_definitions], DESC)
```

then say the term is not defined here, name the nearest terms you saw, and stop. **Do not
compute anything for a term with no definition** - nothing in this tenant agrees what its
number means, and inventing one is exactly what this layer exists to prevent.

## Step 2 - take rank 1

The row with `rank` = 1 is the answer. The ranking already weighed authority, endorsement, how
often the measure is actually queried, how widely it is used, and how fresh its model is. Take
it and move on.

Take a different row **only** when the user named a model or workspace, in which case take
theirs.

Never present the list and ask the user to choose. That is a non-answer.

## Step 3 - run the number

Use the rank-1 row's `workspace_id` and `model_id` in the URL - **not the context model's
ids** - and call the measure by name:

```dax
EVALUATE ROW("v", [<measure>])
```

With a filter:

```dax
EVALUATE ROW("v", CALCULATE([<measure>], '<table>'[<column>] = "<value>"))
```

Over a breakdown:

```dax
EVALUATE TOPN(20, SUMMARIZECOLUMNS('<table>'[<column>], "v", [<measure>]), [v], DESC)
```

Rules for this query:

- **Call the measure by name. Never re-derive its logic.** If a measure exists, `[Measure]` is
  the answer; a SUM or DIVIDE you wrote yourself is a different definition wearing its name.
- One `EVALUATE` per call. Keep results small - use `TOPN`.
- **Never invent a filter literal.** If you are not certain a value exists, fetch it first, on
  the same model:

  ```dax
  EVALUATE TOPN(50, VALUES('<table>'[<column>]))
  ```

  A wrong literal returns a plausible wrong number with no error, which is the worst failure
  available here.
- On an error, fix the query and retry at most twice, then report the error text verbatim.

## Step 4 - answer

Three blocks, in this order. Nothing from a later block may appear in an earlier one.

**1. The number.** Its value, unit and period - then stop. Round it to what a person reads
(1.37 billion MWh, not 1,374,388,307.9978); the exact figure goes in Sources. If the question
asked for a breakdown, this block is the table. No measure name, no model, no GUID, no rank
and no caveat here: the reader must be able to stop after this block and have the answer.

**2. Sources**, under that heading:

- the measure and the model it came from;
- its rank out of how many definitions, and the score;
- the DAX you ran, verbatim, so it can be checked;
- the unrounded value, if you rounded.

If more than one row came back in step 1, say so **here**, in one line: which measure you
used, which model it lives in, and that the others differ. Not beside the number. Do not lay
the rival expressions out, do not invite the reader to choose, and never close by saying the
number would be different under another definition.

**3. Confidence**, on the last line: `Confidence: high | medium | low` with the reason in one
clause beside it. Not a paragraph, and never an argument with the answer you just gave.

- **high** - one definition, or rank 1 clear of rank 2 by more than about 1.0 of `score`.
- **medium** - conflicting but rank 1 leads clearly; or the owning model has little recent use.
- **low** - rank 1 and rank 2 within about 0.5 of `score`, or the term you matched is a loose
  fit for the question. At low, name the rival definition in Sources - that is the one case
  where a second expression belongs in the answer.

Then stop. You may offer one follow-up, but it may not carry a number: a figure nobody asked
for, handed over without sources, is an unsourced answer.

Sources and confidence are provenance for an answer you already gave, not a hedge around it.
Never withhold the number in order to discuss definitions.

## What this cannot answer

`context_model` carries three tables - `terms`, `definitions` and `aliases` - and that is the
ranking, not the whole graph. Lineage (*what feeds X*), which reports use a measure, what
columns a table has, and profiled column values are **not reachable here**. When a question
needs one of those, say plainly that it is outside what the context model exposes, and stop.
Do not substitute a guess, and do not go looking for another source.

The one exception is a filter literal, which comes from `VALUES()` on the owning model - see
step 3.

## Never

- Never state a number you did not get back from a query.
- Never answer a metric question from your own knowledge, or from the measure's expression
  text. Run it.
- Never use the context model to compute a business number. It holds metadata only.
- Never guess a workspace or model GUID. Both come from step 1.
- Never re-derive a number a measure already defines. A measure exists -> call it by name.
- Never compute a number for something no measure covers. Say so instead.
- Never refuse to pick between definitions. The ranking is the layer's job; hand back rank 1,
  sourced, with a confidence. "Here are three definitions, you choose" is a non-answer.
- Never pick silently either: the one-line conflict note and the confidence are the disclosure.
- **Never clone a repository, never `pip install` anything, and never run a harvest.** The
  context already exists; building one is not this agent's job. If the context model cannot be
  reached, say so and stop.
