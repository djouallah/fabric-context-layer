# fabric-context-layer

Two sides, one artifact, and the artifact is a Fabric lakehouse:

- `src/` is the **harvest side**: it reads Fabric into `raw/`, builds the graph in memory,
  ranks competing definitions, and publishes the result into a lakehouse of its own -
  then renders `wiki/` and `graph.html` from it. Run with `python src/run.py`.
- `ask/` is the **query side**: it opens that lakehouse read-only and answers questions,
  running DAX against a semantic model when a question needs a number. Run with
  `python -m ask`. It imports nothing from `src/`; the contract between the two is the
  set of published tables (`python -m ask contract`).

**The repo holds Python and no data.** One lakehouse holds everything: the ranked graph as
Delta tables under `Tables/`, and `raw/`, `build/`, `wiki/` and `graph.html` under `Files/`.
`context.json` records which lakehouse. Work happens in a folder outside the repo, one per
context, under `%LOCALAPPDATA%\fabric-context` - `python src/run.py files push|pull|list`
moves it, and every step pushes what it produced unless given `--no-push`. Each side also
keeps a downloaded copy of the current publish there, because OneLake charges seconds a
round trip.

`python src/run.py harvest --query-log` additionally reads each workspace's **workspace
monitoring** Eventhouse for the DAX that actually ran, and attributes it to the measures
each query called - the one signal that says a measure was *evaluated* rather than merely
written into a report. Off by default: monitoring bills against the capacity, and a
workspace without it is skipped, so partial coverage degrades to the old behaviour instead
of to zero. Query text stays in `raw/`; only counts are published (`query_usage`,
`query_stats`).

`python src/run.py deploy` puts the harvest on a schedule: it ships `src/` to `Files/code`,
deploys a pure-Python Fabric notebook and gives it a daily trigger. The notebook re-runs the
whole pipeline nightly and pushes the delta back, so the laptop is optional.

Questions about the tenant's terms, models, tables, lineage, usage or numbers go through
the `fabric-context` skill (`.claude/skills/fabric-context/SKILL.md`), which uses
`python -m ask`. Do not answer such questions from memory or by reading `raw/` directly.

**Docs invariant:** `README.md` stays super succinct and high level - the idea, the
premises, the hook, the diagram, one pointer line. Every detail goes in `run.md`: how to
run it, the ranking, the schema, what is harvested, the limits. Never grow the README to
explain something; put it in run.md.

Working on the code: `python src/selftest.py` runs the harvest side end to end on a
synthetic tenant, publishing to a temp folder so it needs no network, and must stay green;
`python -m ask contract` must stay `ok` after any change to what `src/graph.py` publishes.
`--json` is a global flag on `python -m ask` and goes before the subcommand.

`python -m ask sql` reads the context tables, and also the data itself when the query
qualifies a table with a harvested store (`coffee.benchmark_tests.contoso_sales`) - the
store attaches read-only over its SQL analytics endpoint through DuckDB's `mssql`
community extension, so context and data join in one statement. It is the fallback for
tables no semantic model covers; a measure's number still comes from DAX. The dialect is
DuckDB, not T-SQL.
