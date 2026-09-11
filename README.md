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

The harvest delivers one thing: a knowledge graph of the tenant - every term, its
competing definitions ranked, and what feeds what. That graph is the context. It runs
nightly on its own, an agent asks whenever, and the two meet at the graph without ever
calling each other.

**[See it on a real tenant](https://djouallah.github.io/fabric-context-layer/)**

![The harvest side reads a Fabric workspace, builds and ranks the graph, and publishes it as the context. Any stateless agent searches and defines a term from that context, runs rank 1 as DAX on the model that owns it, and answers with the number, its source and a confidence.](docs/how-it-works-dark.svg)

The ranking, running it, the nightly refresh, what is harvested, the schema, the limits: **[run.md](run.md)**.

## Licence

MIT - see [LICENSE](LICENSE).
