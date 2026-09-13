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
nightly on its own.

**[See it on a real tenant](https://djouallah.github.io/fabric-context-layer/)**

![Inside the platform, the harvest side reads a Fabric workspace, builds and ranks the graph, and publishes it as the context. Outside it, any stateless agent - on a laptop, in a notebook, in CI, in a chat - reads that context as one markdown file, runs rank 1 as DAX on the model that owns it, and answers with the number, its source and a confidence.](docs/how-it-works-dark.svg)


```python
!pip install fabcontext
import fabcontext
url = fabcontext.harvest("Workspace_A")
```

The first call creates the lakehouse; every later one updates it. It returns the URL an
agent asks against.

The ranking, what is harvested, the schema, how to ask, the limits: **[docs/guide.md](docs/guide.md)**.

## Agent

The agent can be anything - Claude, GitHub Copilot, Scout, Microsoft 365 Copilot. It needs
two things and installs nothing: a read-only Power BI connection, and the ids of the semantic
model the harvest published the ranking into. One DAX query asks that model which definition
wins; a second runs it. One folder per tool: **[agent/](agent/)**.

## Licence

MIT - see [LICENSE](LICENSE).
