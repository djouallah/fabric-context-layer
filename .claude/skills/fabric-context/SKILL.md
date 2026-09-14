---
name: fabric-context
description: Answer a business-metric question with a single number taken from the tenant's ranked definitions - "what is revenue", "what was <metric> for <filter>", "which definition of <term> should I trust". Resolves the term against the published context model, takes the top-ranked definition, and runs that measure by name as DAX on the model that owns it.
---

# Answering from the context layer

A semantic model named **`context_model`** holds every competing definition of every business
term, ranked. You answer by asking it which definition wins, then running the winning measure
on the model that owns it.

**Nothing is installed and nothing is cloned.** The Azure CLI signed in (`az login`) is the
whole setup - the model is at a fixed address, so you find it rather than being handed it.

## Finding the context model

It lives at a fixed address: a semantic model named `context_model`, in a workspace named
`context_layer`. That is true in every tenant, which is why you can find it yourself rather
than asking for it.

If `$FABRIC_CONTEXT_MODEL` is set it already holds both ids as `<workspace-guid>/<model-guid>`
- use it and skip the rest. Otherwise, two calls.

The workspace:

```bash
az rest --method get --resource "https://analysis.windows.net/powerbi/api" \
  --url "https://api.powerbi.com/v1.0/myorg/groups?\$filter=name%20eq%20'context_layer'"
```

The workspace guid is `value[0].id`. If the filter is rejected, drop it and list `/groups`
unfiltered, then pick the row whose `name` is exactly `context_layer` yourself.

Then the model, in that workspace:

```bash
az rest --method get --resource "https://analysis.windows.net/powerbi/api" \
  --url "https://api.powerbi.com/v1.0/myorg/groups/<ws-guid>/datasets"
```

Take the row whose `name` is `context_model`; its `id` is the model guid. That workspace holds
the context layer and nothing else, so the list is short.

Remember both ids for the rest of the conversation and do not look them up twice.

When the lookup comes back empty, say so and stop - do not go looking through other
workspaces:

- **No workspace named `context_layer`** - the context layer has not been published in this
  tenant, or this account cannot see it.
- **The workspace is there but holds no `context_model`** - the ranking is published but the
  model over it was never created. Say that it is missing and that whoever publishes the
  context has to add it. There is nothing else here for you to read, and no second route.

`context_layer` is the one workspace name you look up. If you are pointed at some other
workspace by name, say you cannot look a name up and ask for the model's link instead.

**Never guess or construct a GUID.** If a query answers "Invalid dataset or workspace", say
the ids look wrong and check them again rather than trying variations.

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
EVALUATE FILTER('answers', 'answers'[alias_norm] = "<term>")
```

`answers` is one row per spelling of every term, and the row is the answer already: the
rank-1 `measure`, the `model` that owns it, that model's `workspace_id` and `model_id`, the
`expression`, a ready-to-run `dax`, a `confidence`, `n_definitions`, whether they are
`conflicting`, and `rivals` - the other definitions, ranked, in one line. The ranking is
already decided and is not yours to redo. Take the row and move on. Two rows means the
spelling names two terms; take the one whose `label` fits the question.

If nothing comes back, widen once to see what the layer does know:

```dax
EVALUATE TOPN(15, SELECTCOLUMNS('terms', "term", 'terms'[label],
    "n", 'terms'[n_definitions]), 'terms'[n_definitions], DESC)
```

then say the term is not defined here, name the nearest terms you saw, and stop. **Do not
compute anything for a term with no definition** - nothing in this tenant agrees what its
number means, and inventing one is exactly what this layer exists to prevent.

## Step 2 - the two exceptions

The row from step 1 is the answer. Never present a list and ask the user to choose; that is
a non-answer. Two questions, and only these, go past it to the ranked list:

```dax
EVALUATE
CALCULATETABLE(
    SELECTCOLUMNS('definitions',
        "rank", 'definitions'[rank],
        "measure", 'definitions'[name],
        "model", 'definitions'[owner_item_name],
        "workspace_id", 'definitions'[workspace_id],
        "model_id", 'definitions'[owner_item_id],
        "expression", 'definitions'[expression],
        "confidence", 'definitions'[confidence]),
    'aliases'[alias_norm] = "<term>")
```

- **The user named a model or workspace.** Take their definition's row instead of rank 1.
- **The user asked to compare the definitions.** That is the one question where laying the
  expressions out is the answer: each with its rank and model, then stop - no number.

And one fallback: if the rank-1 model cannot be queried in step 4 - a 403, a 404, a model
that is gone, an error no fix to the query addresses - take the next rank from this list,
say so in Sources, and drop the confidence one level.

## Step 3 - get the real column names

Skip this when the question needs no filter and no breakdown: `EVALUATE ROW("v", [<measure>])`
names no column, so there is nothing to look up.

Otherwise, ask the **owning** model what it contains - the row's `workspace_id` and
`model_id`, the same ids you are about to run the number on:

```dax
EVALUATE INFO.VIEW.COLUMNS()
```

One row per column in the model. **Read the table and column names out of the response's own
headers** rather than assuming what those headers are called, then pick the pair that matches
what the user asked to filter or group by and use it verbatim. If the model is large enough
that the response is unwieldy, re-run it filtered, using the header names you just learned.

If that query errors, fall back to:

```dax
EVALUATE COLUMNSTATISTICS()
```

which names the table and column directly and carries min/max as well - but it scans the
model, so it is the second choice, not the first.

**Never take a table or column name from the measure's `expression` text.** The expression
names what the measure reads, not what you may filter or group by, and the two are routinely
different. A column inferred from DAX you read is a guess, and this is the guess that fails.

## Step 4 - run the number

Use the row's `workspace_id` and `model_id` in the URL - **not the context model's ids** -
and call the measure by name:

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
  A rejected table or column name is not one of those errors: it means step 3 was skipped, or
  its answer was overridden by a guess. Run the discovery query and try again.

## Step 5 - answer

Three blocks, in this order. Nothing from a later block may appear in an earlier one.

**1. The number.** Its value, unit and period - then stop. Round it to what a person reads
(1.37 billion MWh, not 1,374,388,307.9978); the exact figure goes in Sources. If the question
asked for a breakdown, this block is the table. No measure name, no model, no GUID, no rank
and no caveat here: the reader must be able to stop after this block and have the answer.

**2. Sources**, under that heading:

- the measure and the model it came from;
- its rank out of how many definitions, and the score, to two decimals;
- the DAX you ran, verbatim, so it can be checked;
- the unrounded value, if you rounded.

If the row says `conflicting`, say so **here**, in one line: which measure you used, which
model it lives in, its rank out of `n_definitions`, and that the `rivals` differ. Not beside
the number. Do not lay the rival expressions out, do not invite the reader to choose, and
never close by saying the number would be different under another definition.

**3. Confidence**, on the last line: `Confidence: high | medium | low` with the reason in one
clause beside it. Not a paragraph, and never an argument with the answer you just gave.

Take it from the row's own `confidence`; do not re-derive it by comparing `score` values
yourself.

Drop it one level if the term you matched is a loose fit for what was asked - that is a
property of the question, which the row cannot know. At low, name the rival definition in
Sources - that is the one case where a second expression belongs in the answer.

Then stop. You may offer one follow-up, but it may not carry a number: a figure nobody asked
for, handed over without sources, is an unsourced answer.

Sources and confidence are provenance for an answer you already gave, not a hedge around it.
Never withhold the number in order to discuss definitions.

## What this cannot answer

`context_model` carries four tables - `answers`, `terms`, `definitions` and `aliases` - the
ranking, and nothing else. Lineage (*what feeds X*), which reports use a measure, and how
often one is queried are **not reachable here**. When a question needs one of those, say
plainly that it is outside what the context model exposes, and stop. Do not substitute a
guess, and do not go looking for another source.

A model's own structure is the exception, and it does not come from `context_model` at all: the
columns come from the owning model in step 3, and a filter literal from `VALUES()` on that same
model in step 4. Both are questions a model can answer about itself.

## Never

- Never state a number you did not get back from a query.
- Never answer a metric question from your own knowledge, or from the measure's expression
  text. Run it.
- Never use the context model to compute a business number. It holds metadata only.
- Never guess a workspace or model GUID. Both come from step 1.
- Never ask the user for a table or column name before running step 3. The model knows its own
  columns; asking the person who asked you is a non-answer.
- Never re-derive a number a measure already defines. A measure exists -> call it by name.
- Never compute a number for something no measure covers. Say so instead.
- Never refuse to pick between definitions. The ranking is the layer's job; hand back rank 1,
  sourced, with a confidence. "Here are three definitions, you choose" is a non-answer.
- Never pick silently either: the one-line conflict note and the confidence are the disclosure.
- **Never clone a repository, never `pip install` anything, and never try to build the
  context yourself.** It already exists. If the context model cannot be reached, say so and
  stop.
