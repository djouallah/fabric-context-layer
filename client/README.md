# client/ - asking the questions

This is the client side. It reads one semantic model, named `context_model`, and it installs
nothing. How that model comes to exist is the other side of the repo, and it is described
in [../docs/guide.md](../docs/guide.md), not here.

## What an agent needs

One thing, and no more:

**A read-only Power BI connection.** `az login` on a laptop; the first-party Power BI
connector in Copilot Studio. Plus the tenant setting *Dataset Execute Queries REST API*, and
Read + Build on the models. Every query runs as the person asking, so RLS applies and two
people can correctly get different numbers.

There are no ids to configure. `context_model` lives in a workspace named `context_layer`, the
same in every tenant, so an agent with a shell looks it up. M365 Copilot has no shell and is
the exception: it is given the model's two GUIDs when the agent is built.

No clone, nothing installed, no OneLake, no local database. The ranking lives in the model,
so nothing on this side changes when it changes.

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

`context_model` exposes the ranking - one table, `answers` - and nothing else.
So this side answers *what is revenue*, *what was X for Y*, and *which definition should I
trust*. It cannot answer *what feeds revenue*, *which reports use it*, or *what is in
table T*. The skill says so and stops rather than guessing.

## Before you trust it

**Ask something you already know the answer to, and check it by hand.** The agent still writes
the final DAX. A mistaken filter returns a plausible number with no error at all, which is the
one failure here that does not announce itself.
