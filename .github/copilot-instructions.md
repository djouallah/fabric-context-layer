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
- The skill's "Where it runs" says what the commands need - Python 3.12, the root of this
  clone, `pip install -e .`, `az login`, `--db` - and when to stop instead. Two things it
  cannot know about this surface:
  - In ask mode you cannot run commands: say the question needs agent mode and stop.
  - The cloud agent has no `az login`: say so and stop; do not set up credentials.

`agent/` is the other side of this repo: how someone with no clone points a tool at an
already-published context, over Power BI alone. It is documentation of that setup, not
instructions for you - do not paste or follow `agent/SKILL.md` or `agent/m365/instructions.md`
here. In this clone you use the skill in `.claude/skills/`, which reads `context.md`.

## Working on the code

- `pytest` must stay green. Use a venv built from `requirements-dev.txt`, which pins duckdb
  and deltalake to the versions the Fabric runtime ships.
- duckrun is never a dependency; `tests/test_no_duckrun.py` is the gate.
- Every dependency, and its floor, must be in `docs/fabric-runtime.txt`.

Everything else - the ranking, the schema, what is harvested, the limits - is in
`docs/guide.md`.
