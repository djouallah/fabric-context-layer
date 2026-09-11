# fabric-context-layer

A context layer for Microsoft Fabric, written to learn how one works.

## What a context layer is

A context layer sits above semantic models. It defines nothing itself: it reads the
models, reports, notebooks, pipelines and usage a tenant already has, and works out from
them what each business term means, who defines it, who uses it and what feeds it. A
definition *is* a measure in a semantic model. When two models define the same term with
different DAX, the layer ranks the two and picks one. When a question needs a number, the
ranked measure is run against its own model, by name; the layer never re-derives the logic.

So it is only as good as the models under it. A term with no measure has no definition. A
measure with no description, no endorsement and no report using it gives the ranking
nothing to weigh. A context layer is what you put on top of solid semantic models, not a
way around building them.

This repo is a small implementation, meant to be read. The harvest is one Python file per
step, the graph is two tables, the ranking is one SQL statement, and the query side is a
command line an agent can drive. Run it against your own workspace and look at the graph,
the wiki and the ranking to see what a context layer sees, and what it cannot.

**[See it running](https://djouallah.github.io/fabric-context-layer/)** on a real tenant:
1,798 nodes, 2,920 edges. A term ringed in red is defined two different ways by two
different models; click it to see which definition won and the DAX behind it.

## The ranking

Each measure's name normalises to a term: stop words dropped, common synonyms folded
(`average`, `avg`, `mean`), plurals stripped, and the remaining words **sorted**, so
`Average Price`, `Price_AVG` and `Avg Price` are one term. `src/aliases.yaml` (optional)
pins the merges the word lists cannot make. When two measures define one term with
different DAX, that is a conflict, and every definition of the term is scored on four
signals:

| signal | weight | how it is computed |
|---|---|---|
| authority | 2.0 | certified 2, promoted 1; +0.5 documented; +0.5 defined in a model rather than a report |
| popularity | 1.5 | `ln(1 + 28-day opens of the reports that use it + times the query log saw it evaluated, where the workspace is monitored + opens, queries and refreshes of the model that defines it)` |
| relevance | 1.0 | `ln(1 + reports) + 0.25 ln(1 + visuals)`; +0.5 if the name is exactly the term |
| freshness | 0.5 | `exp(-days since the owning item changed / 180)` |

Rank 1 is the definition. The weights are in `src/graph.py` and are hand-picked, not
learned. **Rank is not correctness.** A popular, certified, wrong definition still ranks
first. The wiki says so on every term page, and the query side says so in every answer to
a conflicting term.

What the query side does with it: `search` finds the term by any of its spellings,
`define` returns its definitions ranked, and `dax` runs a query that calls rank 1 by name
on the model that owns it. The answer gives the number first, then the measure, model,
rank and score it came from, then a confidence read off the gap between rank 1 and
rank 2. A conflict is one line in the answer, not a menu. The full protocol is in
[ask/README.md](ask/README.md) and the `fabric-context` skill.

## How it works

Two sides that never call each other. The harvest runs on its own, nightly; an agent asks
whenever it likes; they meet at one Fabric lakehouse.

```
   harvest side - runs on its own, nightly            query side - runs when asked

Fabric workspace                                      agent (Claude Code + the skill)
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

The repo holds Python and no data. One lakehouse holds the whole context: the ranked graph
as Delta tables under `Tables/`, and what it was built from and rendered into under
`Files/` - the harvested JSON, the parsed edges, the wiki, the graph page. The query side
imports nothing from `src/`; the published tables are the only link between the two, and
`python -m ask contract` checks they are there.

Running it, asking it, the nightly refresh, what is harvested, the graph schema, what it
needs and the known limits: **[run.md](run.md)**.

## Licence

MIT - see [LICENSE](LICENSE).
