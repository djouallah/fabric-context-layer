# fabric-context-layer

Two sides, one artifact, and the artifact is a Fabric lakehouse:

- `fabcontext/` is the **harvest side**, and it is a pip package (`pip install fabcontext`).
  It reads Fabric into `raw/`, builds the graph in memory, ranks competing definitions, and
  publishes the result into a lakehouse of its own - then renders `wiki/` and `graph.html`
  from it. One call: `import fabcontext; url = fabcontext.harvest("My Workspace")`, or
  `python -m fabcontext "My Workspace"`.
- `ask/` is the **query side**: it opens that lakehouse read-only and answers questions,
  running DAX against a semantic model when a question needs a number. Run with
  `python -m ask --db <url>`. The URL `harvest()` returned is the entire contract between
  the two; `ask` imports `fabcontext._fabric` for tokens and Delta reads and nothing else.

**The repo holds Python and no data.** One lakehouse holds everything: the ranked graph as
Delta tables under `Tables/`, and `raw/`, `build/`, `wiki/` and `graph.html` under `Files/`.
A run works in a temp folder and keeps nothing on the machine - there is no `context.json`
and no cache of the publish on the harvest side.

**Create or update.** The first call makes the lakehouse; every later call refreshes it. A
run against an existing lakehouse pulls `Files/raw` down first, which is what makes the
harvest incremental.

**duckrun is not a dependency and must never become one.** It requires duckdb >= 1.5.4 and
deltalake == 1.5.0 exactly, while the Fabric Python 3.12 runtime ships 1.4.4 and 1.2.1, so
importing it would mean pip replacing two native libraries and a `restartPython()` before
anything could run. What it used to provide is vendored in `fabcontext/_fabric/` - tokens,
REST, the workspace handle, OneLake files, Delta I/O, the TMSL patterns. `tests/test_no_duckrun.py`
is the gate, and it is easy to defeat by accident because duckrun *is* installed on this
machine at `C:\duckrun`.

**Every dependency must already be in the Fabric runtime** (`docs/fabric-runtime.txt`), with
the floor at or below the version listed there. That is what makes `%pip install fabcontext`
a single cell with no restart. Adding a dependency that is not on that list, or raising a
floor past it, breaks the one property the package exists for.

`harvest(..., query_log=True)` additionally reads each workspace's **workspace monitoring**
Eventhouse for the DAX that actually ran, and attributes it to the measures each query
called - the one signal that says a measure was *evaluated* rather than merely written into
a report. Off by default: monitoring bills against the capacity, and a workspace without it
is skipped, so partial coverage degrades to the old behaviour instead of to zero. Query text
stays in `raw/`; only counts are published (`query_usage`, `query_stats`).

There is no deployer or scheduler. Put the two cells in a notebook and schedule that notebook
from Fabric.

Questions about the tenant's terms, models, tables, lineage, usage or numbers go through the
`fabric-context` skill (`.claude/skills/fabric-context/SKILL.md`), which uses `python -m ask`.
Do not answer such questions from memory or by reading `raw/` directly.

**Docs invariant:** `README.md` stays super succinct and high level - the idea, the premises,
the hook, the diagram, one pointer line. Every detail goes in `run.md`: how to run it, the
ranking, the schema, what is harvested, the limits. Never grow the README to explain
something; put it in run.md.

Working on the code: `pytest` runs the harvest end to end on a synthetic tenant, publishing
to a temp folder so it needs no network, and must stay green. Use a venv built from
`requirements-dev.txt`, which pins duckdb and deltalake to the Fabric versions - a laptop
otherwise resolves newer ones and the suite stops saying anything about production.
`tests/test_publish.py` carries the published column contract, copied from `ask/context.py`;
it must stay satisfied after any change to what `fabcontext/graph.py` publishes.
`--json` is a global flag on `python -m ask` and goes before the subcommand.

`python -m ask sql` reads the context tables, and also the data itself when the query
qualifies a table with a harvested store (`coffee.benchmark_tests.contoso_sales`) - the store
attaches read-only over its SQL analytics endpoint through DuckDB's `mssql` community
extension, so context and data join in one statement. It is the fallback for tables no
semantic model covers; a measure's number still comes from DAX. The dialect is DuckDB, not
T-SQL.
