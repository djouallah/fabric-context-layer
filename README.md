# context-layer

There is a lot of talk about context layers, and the best way to learn one is to build
one. This is a toy, but the core ideas turned out to be simple.

- A data platform is full of signals: query history, who opens what, what is certified,
  what is refreshed. That is enough to rank definitions.
- Nobody will maintain a knowledge graph by hand, and the ones that exist drift. The
  graph has to build itself.
- A stateless agent is attractive: the ranking happens before the question, so the model
  behind the agent can change without losing anything.
- A context layer is there to be useful, not to hold the ultimate truth.

The bet is that a context built automatically, at least to start, and ranked on how the
platform already behaves gets better as the platform is used. I do not know yet whether
that holds. What I do like is that it uses the semantic models already there and asks for
no new modelling.

So far the industry splits on who settles a conflict: the platform, by scoring it,
which scales with usage, or a person, by reviewing it, which scales with reviewer time; I
prefer the first, time will tell.

Ask three semantic models what *average price* is and you get three answers. This harvests
a Microsoft Fabric tenant, finds every definition of every business term, and ranks them
the way web search ranks pages. **[See it on a real tenant](https://djouallah.github.io/fabric-context-layer/)**:
a term ringed in red is defined two ways; click it to see which won and the DAX behind it.

The harvest runs nightly on its own. An agent asks whenever. They meet at the context and
never call each other.

![The harvest side reads a Fabric workspace, builds and ranks the graph, and publishes it as the context. Any stateless agent searches and defines a term from that context, runs rank 1 as DAX on the model that owns it, and answers with the number, its source and a confidence.](docs/how-it-works-dark.svg)

The ranking, running it, the nightly refresh, what is harvested, the schema, the limits: **[run.md](run.md)**.

## Licence

MIT - see [LICENSE](LICENSE).
