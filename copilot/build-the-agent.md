# Building the agent by asking for it

Copilot Studio can build the agent conversationally, so none of this is a click path. Paste
the block below into its builder, then paste [instructions.md](instructions.md) - everything
below its `---` - as the very next message.

---

Create an agent called "Metric Answers".

It answers business-metric questions with a single number taken from our tenant's ranked
metric definitions in Microsoft Fabric.

First, set up its tool: add the Power BI connector and enable the action
"Run a query against a dataset" (ExecuteDatasetQuery). Create the connection now and show me
the sign-in window so I can pick the right tenant and account. The connection must use the
signed-in user's own credentials, not a shared or service account, so row-level security
applies. Confirm to me which account and tenant the connection ended up on before you carry on.

That action is the agent's only tool. Do not add any knowledge sources, files, SharePoint
sites, or web search - everything it needs comes from that one tool.

Then set the agent's instructions to the text I paste next. Use it verbatim: do not summarise,
shorten, reword, reformat or "improve" it. It is a protocol, and paraphrasing it breaks the
guarantee it exists for. Tell me the exact character count you stored so I can check nothing
was truncated.

---

## Two things the prompt cannot do for you

**The sign-in window is interactive by nature.** No wording makes it automatic - it is you
proving who you are - so that is the one dialog you will click. Check the account it lands on:
the whole design rests on the connection running as the person asking, which is what makes
row-level security apply and lets two people correctly get different numbers.

**Builders rewrite instructions they are handed.** That is why the prompt asks for the stored
character count back. Compare it against the file:

```bash
awk '/^---$/{f=1;next} f' copilot/instructions.md | wc -c
```

Meaningfully short means it paraphrased. Paste again and insist on verbatim - a summarised
protocol still sounds right and quietly stops taking rank 1.

## Then ask it something

```
Context model: https://app.fabric.microsoft.com/groups/<workspace-id>/datasets/<model-id>
How many archive files are there?
```

The agent asks for that link itself if you leave it out. Answer a question you already know
the answer to first - run the same two queries by hand with `python -m ask dax` and compare -
because a wrong number here looks exactly like a right one.
