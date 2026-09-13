# Answering from Microsoft 365 Copilot

The context layer as a Copilot agent: a colleague asks *"what was revenue last quarter"* in
Teams or M365 Chat and gets the number, from the ranked definition, with nothing installed on
their machine.

Nothing here is hosted, registered or operated. Two pieces do the work and both already
exist: the first-party **Power BI connector**, whose *Run a query against a dataset* action
takes flat `groupid` / `datasetid` / `query` parameters and
[runs under the signed-in user's own credentials](https://learn.microsoft.com/en-us/microsoft-copilot-studio/advanced-connectors);
and a **semantic model over the context lakehouse**, which is where the ranking lives.

That second piece is the point. The obvious alternative - pasting the ranked terms into the
agent's instructions - hits an 8,000-character ceiling, freezes the ranking at publish time
and dies on a large tenant. Put the ranking in a model and the instructions carry only the
protocol and two GUIDs, and **a re-harvest updates what the agent knows with no republish**.

## 1. The context semantic model

**The harvest builds it.** Every run creates or updates a Direct Lake semantic model called
`context_model` beside the tables, so there is nothing to click and nothing to rebuild when the
ranking changes:

```python
import fabcontext
fabcontext.harvest("My Workspace")
```

The run prints the model's id on its `semantic model` step - that is the `datasetid` the agent
needs, and the workspace is the one you harvested into.

What it builds, and why it is shaped this way ([fabcontext/semantic_model.py](../fabcontext/semantic_model.py)):

```
aliases [*] ──┐
              ├──> terms [1] ──> [*] definitions
              │    term_id          term_id, rank, score, conflicting
              │    label            name, workspace_id, owner_item_id
              │    n_definitions    owner_item_name, table_name, expression
              │    conflicting
```

- `definitions` is the fact - one row per competing definition, carrying `rank` and `score`.
- `terms` is the dimension, one row per business term.
- `aliases` holds every spelling of every term, which is what lets a lookup by the user's own
  wording land.
- `workspace_id` and `owner_item_id` are the two GUIDs the connector needs to run the measure
  on the model that owns it.

The `aliases` relationship is **bidirectional**, deliberately. Filters flow one-to-many, so
filtering a spelling on the many side would never reach `terms`, let alone propagate on to
`definitions`, and every lookup by an alias would come back empty - which an agent reads as
"the term is not defined" rather than as a bug.

`definitions` also carries a `Dax Template` measure returning a ready-to-run
`EVALUATE ROW("v", CALCULATE([<measure>]))` for the definition in filter context, so an agent
can copy the query rather than compose one.

Because the tables are Direct Lake, a later harvest refreshes what the model serves with no
reload and no republish of the agent. Creating the model needs a permission publishing the
tables did not; if the tenant refuses it, the run says so and the context is still published.

Sanity-check it with the lookup the agent will run:

```dax
EVALUATE
CALCULATETABLE(
    SELECTCOLUMNS('definitions',
        "rank", 'definitions'[rank], "measure", 'definitions'[name],
        "model", 'definitions'[owner_item_name], "model_id", 'definitions'[owner_item_id]),
    'aliases'[alias_norm] = "revenue")
```

A contested term returns several rows, ranked 1..n.

## 2. The agent

In Copilot Studio, published to M365 Copilot:

1. Paste [copilot/instructions.md](../copilot/instructions.md) - everything below the `---` -
   into the agent's instructions. Nothing to fill in: the first time it needs them, the agent
   asks whoever is talking to it for a link to the `context_model` semantic model and reads
   both ids out of the address. It has to ask, because the Power BI connector has no action
   that lists workspaces or models - and a name cannot be resolved without one.
2. Add the **Power BI** connector's *Run a query against a dataset* as a tool. Confirm the
   connection is set to user credentials.
3. Publish.

Keep the instructions file in the repo in sync with the agent. It is the only versioned part -
the agent itself lives in the portal, which is an accepted cost, because the thing that
actually changes between harvests is the ranking, and that lives in the model.

## 3. What the tenant needs

- The **Dataset Execute Queries REST API** tenant setting enabled (Admin portal → Integration
  settings).
- Each user holding **read + build** on the context model and on the models they ask about -
  the same permission the answer should respect anyway. Because the connector runs as the
  signed-in user, RLS applies and two people can correctly get different numbers.
- Each user creates their own connection on first use: the Power Platform connection is not
  shareable. It is a one-click consent, not an admin task.

## Cost

Copilot Studio bills an agent action at 5 credits, but usage by a user holding an M365 Copilot
seat - and of the tools those agents invoke - is
[included in that seat](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-messages-management).
A consumer *without* a seat costs roughly 7 credits per question, and there is no middle
setting; that is the number to know before a wide rollout.

## Limits

- **The agent still writes the final DAX.** The instructions hand it the skeleton and forbid
  invented filter literals, but a wrong literal returns a plausible wrong number with no
  error. The answer echoes the query so it can be checked. This is the real cost of having
  nothing hosted in between.
- The orchestrator reasons over the tool's output and may reword or round it.
- Not available in sovereign clouds (GCC/GCCH/DoD).

## Why not the two easier-looking options

**`context.md` as a SharePoint knowledge source.** Copilot
[cannot parse tables in SharePoint content](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/optimize-content-retrieval),
retrieval is relevance-chunked rather than whole-file, and Microsoft
[explicitly forbids](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-instructions)
directive content in a knowledge source - "take rank 1" is exactly that, and may be sanitized
at runtime. A ranked table is the worst possible shape for that channel.

**A Fabric data agent.** It cannot express a ranking: its DAX generation
[ignores agent-level instructions](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices),
and Microsoft's documented remedy for competing measures is to exclude the duplicates from the
AI data schema. Deletion, not ranking - the opposite of the premise here.
