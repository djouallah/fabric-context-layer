# agent/ - asking the questions

The harvest side built the context and published it. This side asks it things, and it
installs nothing.

## What an agent needs

Two things, and no more:

1. **A read-only Power BI connection.** `az login` on a laptop; the first-party Power BI
   connector in Copilot Studio. Plus the tenant setting *Dataset Execute Queries REST API*,
   and Read + Build on the models. Every query runs as the person asking, so RLS applies and
   two people can correctly get different numbers.
2. **The context model's ids** - `<workspace-guid>/<model-guid>` for the `context_model`
   semantic model the harvest published beside the lakehouse. Open it in Fabric and the
   address carries both: `/groups/<workspace-guid>/datasets/<model-guid>`.

No clone, no `pip install`, no OneLake, no local database. The ranking lives in a semantic
model, so **a re-harvest updates what the agent knows with nothing republished**.

## How a question is answered

Two DAX queries.

1. Against `context_model`: which definition of this term wins, and which model owns it. The
   row carries the measure's name and the two GUIDs to run it against.
2. Against that model: the measure, called by name, in whatever filter context the question
   asked for.

Then the answer, in three blocks - the number alone, then Sources, then one line of
confidence. Calling the measure by name is the point: a SUM you wrote yourself is a different
definition wearing its name.

## Pick your tool

| | |
|---|---|
| [claude/](claude/) | Claude Code, Claude Desktop |
| [copilot/](copilot/) | GitHub Copilot - VS Code, the CLI, the cloud agent |
| [scout/](scout/) | Microsoft Scout - one pasted prompt |
| [m365/](m365/) | Microsoft 365 Copilot - a Copilot Studio agent, nothing installed at all |

The first three drive a shell, so they all install the same file, [SKILL.md](SKILL.md), and
differ only in where it goes. M365 Copilot has no shell - its one tool is the Power BI
connector - so it gets [m365/instructions.md](m365/instructions.md), the same protocol written
for a portal field with an 8,000-character cap.

## The limit worth knowing first

`context_model` exposes the ranking - `terms`, `definitions`, `aliases` - and not the whole
graph. So this side answers *what is revenue*, *what was X for Y*, and *which definition
should I trust*. It cannot answer *what feeds revenue*, *which reports use it*, or *what is in
table T*; those live in `context.md` and need the clone-side `python -m ask` (see
[../docs/guide.md](../docs/guide.md)). The skill says so and stops rather than guessing.

## Before you trust it

**Ask something you already know the answer to, and check it by hand.** The agent still writes
the final DAX. A mistaken filter returns a plausible number with no error at all, which is the
one failure here that does not announce itself.
