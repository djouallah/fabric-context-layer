# fabric-context-layer

Two sides, one artifact, and the artifact is a Fabric lakehouse:

- `fabcontext/` is the **harvest side**, and it is a pip package (`pip install fabcontext`).
  It reads Fabric into `raw/`, builds the graph in memory, ranks competing definitions, and
  publishes the result into a lakehouse of its own - then renders `wiki/`, `context.md` and
  `graph.html` from it. One call: `import fabcontext; url = fabcontext.harvest("My Workspace")`, or
  `python -m fabcontext "My Workspace"`.
- `ask/` is the **query side**, and it is two commands: `python -m ask context` fetches
  `Files/context.md` in one round trip, and `python -m ask dax <ws-guid>/<model-guid>
  "EVALUATE ..."` runs a number. Nothing else. The client opens no database - no duckdb, no
  deltalake, no cached copy - because the markdown is the metadata and the model ids are in
  it. The URL `harvest()` returned is the entire contract between the two.

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

**Release rule, no exceptions:** a new version of `fabcontext` bumps **only the last digit**
- 0.1.0 -> 0.1.1 -> 0.1.2. Never the middle number, never the first. Set it in both
`pyproject.toml` and `fabcontext/__init__.py`, commit, then tag `v<version>` and push the
tag; `.github/workflows/publish.yml` fires on `v*` and publishes to PyPI by trusted
publishing, so the number cannot be taken back. The package's whole promise is one
`%pip install` with no restart - a bigger number would advertise a break it must never make.

**Docs invariant:** `README.md` stays super succinct and high level - the idea, the premises,
the hook, the diagram, one pointer line. Every detail goes in `docs/guide.md`: how to run it,
the ranking, the schema, what is harvested, the limits. Never grow the README to explain
something; put it in the guide.

Working on the code: `pytest` runs the harvest end to end on a synthetic tenant, publishing
to a temp folder so it needs no network, and must stay green. Use a venv built from
`requirements-dev.txt`, which pins duckdb and deltalake to the Fabric versions - a laptop
otherwise resolves newer ones and the suite stops saying anything about production.
`tests/test_publish.py` carries the published column contract, copied from `ask/db.py`;
it must stay satisfied after any change to what `fabcontext/graph.py` publishes.
`--json` is a global flag on `python -m ask` and goes before the subcommand.

**There is no SQL on the client, and adding one would undo the point.** A number comes from
DAX calling a ranked measure by name, or it does not come. A table no semantic model covers
has nothing in the tenant that agrees what its number means, so the answer is to say that
and describe what is known - not to compute it and imply a definition nobody wrote. The
published tables are still read by `ask/db.py`, but only so `ask/evals.py` can build its
benchmark questions out of the graph; `tests/test_client_is_thin.py` is the gate that keeps
that off the client.
