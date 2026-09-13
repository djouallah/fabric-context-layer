# Building the agent

**It has to be [Copilot Studio](https://copilotstudio.microsoft.com).** Two other surfaces look
like the right place and are not:

- **Microsoft 365 Copilot chat** cannot build agents at all. Ask the assistant to create one
  and it correctly refuses - it has no tool for it.
- **Agent Builder** (in M365 Copilot, *Agents → Create an agent*) is the friendlier UI and
  builds an agent by conversation, but it only attaches **knowledge sources**. Microsoft's own
  guidance: *"If you need more advanced capabilities like Actions to integrate external
  services, use Microsoft Copilot Studio."* This agent's whole job is running a DAX query, so
  knowledge-only cannot do it - an agent that can describe definitions but not return a number
  is not this thing.

You can still start in Agent Builder if you prefer its flow, but the tool has to be attached in
Copilot Studio afterwards.

## 1. Create it, and paste the instructions

Copilot Studio → **Create** → **New agent**. Describe it:

```
An agent called "Metric Answers" that answers business-metric questions with a single number
taken from our tenant's ranked metric definitions in Microsoft Fabric. It always takes the
top-ranked definition of a term and calls that measure by name, and it never adds up a number
of its own. Do not add any knowledge sources, files, SharePoint sites or web search.
```

Then open **Instructions** and paste [instructions.md](instructions.md) - everything below its
`---` - **verbatim**. Don't let the builder summarise or reword it: it is a protocol, and a
paraphrase still sounds right while quietly ceasing to take rank 1. Check that what landed is
the length that left:

```bash
awk '/^---$/{f=1;next} f' agent/m365/instructions.md | wc -c
```

## 2. Attach the tool

**Tools → Add a tool → Connector → Power BI → Run a query against a dataset.**

Create the connection when prompted and sign in - that dialog is interactive by nature, since
it is you proving who you are. Check which account it lands on. The whole design rests on the
connection running as the person asking: that is what makes row-level security apply and lets
two people correctly get different numbers. User credentials, never a shared or service
account.

Add nothing else. That one action is the agent's only tool.

## 3. Publish, then ask it something

Publish to Microsoft 365 Copilot. Pick the agent there and ask:

```
Context model: https://app.fabric.microsoft.com/groups/<workspace-id>/datasets/<model-id>
How many archive files are there?
```

It asks for that link itself if you leave it out - it has no way to find the model on its own.

**Ask something you already know the answer to first.** Run the same two queries by hand with
`python -m ask dax` and compare, because a wrong number here looks exactly like a right one:
the agent still writes the final DAX, and a mistaken filter returns a plausible figure with no
error at all.
