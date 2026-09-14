# fabric-context-layer

`fabcontext/` harvests a Microsoft Fabric tenant, ranks every competing definition of every
business term, publishes the result into a lakehouse of its own and creates a semantic model,
`context_model`, over the ranking. `client/` is the client side: one DAX query against that
model says which definition wins, a second runs it. The repo holds Python and no data.

## Questions about the tenant

Any question about the tenant's terms, models or numbers - "what is revenue", "what was
<metric> for <filter>", "which definition should I trust" - goes through the `fabric-context`
Agent Skill, loaded on its own from `.claude/skills/`. It is the client protocol: find
`context_model`, ask it which definition wins, call that measure by name in DAX on the model
that owns it, answer number-first with sources and a confidence. Follow it as loaded; do not
paraphrase it from memory.

- Never answer such a question from memory, from `Files/raw`, or by adding SQL. A number
  comes from DAX calling a ranked measure by name, or it does not come.
- The skill needs a shell and `az login`. In ask mode you cannot run commands: say the
  question needs agent mode and stop. The cloud agent has no `az login`: say so and stop; do
  not set up credentials.

`context.md` and `wiki/`, in the lakehouse's `Files/`, are the context layer in full, for a
person reading it. They are not the client path.

## Working on the code

- `pytest` must stay green. Use a venv built from `requirements-dev.txt`, which pins duckdb
  and deltalake to the versions the Fabric runtime ships.
- duckrun is never a dependency; `tests/test_no_duckrun.py` is the gate.
- Every dependency, and its floor, must be in `docs/fabric-runtime.txt`.

Everything else - the ranking, the schema, what is harvested, the limits - is in
`docs/guide.md`.
