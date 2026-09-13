# fabric-context-layer

`fabcontext/` harvests a Microsoft Fabric tenant, ranks every competing definition of every
business term, and publishes the result into a lakehouse of its own. `ask/` is the query side,
and it is two commands: `python -m ask context` fetches the whole context as one markdown
file, and `python -m ask dax <ws-guid>/<model-guid> "EVALUATE ..."` runs a number. The repo
holds Python and no data.

## Questions about the tenant

Any question about the tenant's terms, models, tables, reports, lineage, usage or numbers -
"what is revenue", "what was <metric> for <filter>", "what feeds X" - goes through the
`fabric-context` Agent Skill, which is loaded on its own from `.claude/skills/`. It is the
protocol: fetch the context, find the term, take rank 1, call that measure by name in DAX,
answer number-first with sources and a confidence. Follow it as loaded; do not paraphrase it
from memory.

- Never answer such a question from memory, from `Files/raw`, or by adding SQL. A number
  comes from `python -m ask dax` calling a ranked measure by name, or it does not come.
- If you cannot run commands in this mode, say the question needs agent mode and stop.

## Before the first `python -m ask`

- Python 3.12, and run from the repo root: `ask/` is not an installed package
  (`pyproject.toml` ships `fabcontext*` only).
- `pip install -e .` once. `ask` imports `fabcontext._fabric` lazily, so without it the first
  call fails, not the import.
- `az login`, which is where the Power BI token comes from. With no login available (the
  cloud agent, CI), say so and stop; do not set up credentials or a device-code flow.
- `--db` is global and goes before the subcommand. When `context.json` is missing at the repo
  root (it is gitignored), `context` exits 2 with "nothing published yet": ask the user for
  the `abfss://` URL `fabcontext.harvest()` returned, or `<ws-guid>/<lakehouse-guid>`. Do not
  search the filesystem for it, and never run `python -m fabcontext` to make one.

`copilot/` and `docs/copilot.md` are for Microsoft 365 Copilot - a Copilot Studio agent over
the Power BI connector. They are not instructions for you; do not paste or follow them here.

## Working on the code

- `pytest` must stay green. Use a venv built from `requirements-dev.txt`, which pins duckdb
  and deltalake to the versions the Fabric runtime ships.
- duckrun is never a dependency; `tests/test_no_duckrun.py` is the gate.
- Every dependency, and its floor, must be in `docs/fabric-runtime.txt`.

Everything else - the ranking, the schema, what is harvested, the limits - is in
`docs/guide.md`.
