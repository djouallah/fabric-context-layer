# fabric-context-layer

A context layer for Microsoft Fabric, written to learn how one works.

## The idea

A context layer sits above the semantic models and the data. It harvests the metadata a
tenant already has - models, reports, notebooks, pipelines, usage - and builds a context
from it: a knowledge graph of what each business term means, who defines it, who uses it
and what feeds it. Call it an automatic ontology if you like; nobody authors it. When two
models define the same term differently, the definitions are ranked the way web search
ranks pages: authority, popularity, relevance, freshness.

An agent then reads that context instead of the raw metadata, and should answer more
accurately for it. The agent is stateless and the model behind it does not matter: the
ranking happened before any question was asked, so any agent that can run a command line
gets the same rank 1 and runs the same measure. The value is in the context, not in the
agent.

It is only as good as the models under it. A term with no measure has no definition; a
measure nobody documented, endorsed or used gives the ranking nothing to weigh. A context
layer goes on top of solid semantic models, not around them.

This repo is a small implementation, meant to be read: one Python file per harvest step,
a graph of two tables, a ranking in one SQL statement, a query side that is a command
line. **[See it running](https://djouallah.github.io/fabric-context-layer/)** on a real
tenant: 1,798 nodes, 2,920 edges. A term ringed in red is defined two ways by two models;
click it to see which won and the DAX behind it.

## The ranking

A measure's name normalises to a term - stop words dropped, synonyms folded, plurals
stripped, words sorted - so `Average Price`, `Price_AVG` and `Avg Price` are one term.
Two measures on one term with different DAX is a conflict, and every definition is scored:

| signal | weight | how it is computed |
|---|---|---|
| authority | 2.0 | certified 2, promoted 1; +0.5 documented; +0.5 defined in a model rather than a report |
| popularity | 1.5 | `ln(1 + 28-day opens of the reports that use it + times the query log saw it evaluated, where the workspace is monitored + opens, queries and refreshes of the model that defines it)` |
| relevance | 1.0 | `ln(1 + reports) + 0.25 ln(1 + visuals)`; +0.5 if the name is exactly the term |
| freshness | 0.5 | `exp(-days since the owning item changed / 180)` |

Rank 1 is the definition. The weights are in `src/graph.py`, hand-picked, not learned.
**Rank is not correctness.** A popular, certified, wrong definition still ranks first, and
every answer to a conflicting term says so in one line.

When a question needs a number, the agent runs rank 1 by name against the model that owns
it - never a re-derivation of its logic - and answers with the number, then the measure,
model, rank and score, then a confidence read off the gap to rank 2. The protocol is in
[ask/README.md](ask/README.md).

## How it works

The harvest runs on its own, nightly. An agent asks whenever. They meet at one Fabric
lakehouse and never call each other.

```
   harvest side - runs on its own, nightly            query side - runs when asked

Fabric workspace                                        any agent, stateless
  models, reports, notebooks,                           "what was avg price in NSW?"
  pipelines, usage, query log                                     |
         |                                                        v
         v  python src/run.py all                           python -m ask
     raw/ -> graph -> rank                             search -> define -> rank 1
         |                                                        |
         v                                                        |
+-----------------------------+         reads                     |
|  one Fabric lakehouse       | <---------------------------------+
|  Tables/  the ranked graph  |                                   |
|  Files/   raw, wiki, graph  |                                   v  DAX, calling rank 1 by name
+-----------------------------+                       the semantic model that owns it
                                                                  |
                                                                  v
                                                       number + source + confidence
```

The repo holds Python and no data. The lakehouse holds the whole context: the ranked graph
as Delta tables under `Tables/`, and what it was built from and rendered into under
`Files/`. The query side imports nothing from `src/`; the published tables are the only
link, and `python -m ask contract` checks they are there.

Running it, asking it, the nightly refresh, what is harvested, the graph schema, what it
needs and the known limits: **[run.md](run.md)**.

## Licence

MIT - see [LICENSE](LICENSE).
