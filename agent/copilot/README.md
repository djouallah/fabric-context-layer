# GitHub Copilot

Copilot loads Agent Skills from the **workspace** you have open, at
`.claude/skills/<name>/SKILL.md` - the same place Claude Code looks. That is per-workspace,
not per-user, so it goes in whichever folder you ask questions from. A folder holding nothing
but the skill is a perfectly good one.

## Setup

```bash
mkdir -p .claude/skills/fabric-context
curl -fsSL -o .claude/skills/fabric-context/SKILL.md \
  https://raw.githubusercontent.com/djouallah/fabric-context-layer/main/agent/SKILL.md

az login
export FABRIC_CONTEXT_MODEL="<workspace-guid>/<model-guid>"   # from context_model's address
```

Then open that folder in VS Code, or `cd` into it for the CLI, and ask a question in plain
English.

## Send the question to the skill

A skill is loaded, not obeyed. Copilot decides when a question is one the skill covers, and
without a nudge it will sometimes answer a metric question from the repo it can see. Add a
`.github/copilot-instructions.md` to the same workspace:

```markdown
Any question about the tenant's terms, metrics, models or numbers - "what is revenue",
"what was <metric> for <filter>", "which definition should I trust" - goes through the
`fabric-context` skill, loaded from `.claude/skills/`. Follow it as loaded; do not
paraphrase it from memory, and never answer such a question from your own knowledge.
```

## Two surfaces that cannot do it

- **Ask mode** runs no commands, so it cannot query anything. It should say the question
  needs agent mode, and stop.
- **The cloud agent** has no `az login` and no Azure CLI, so it has no Power BI token. It
  should say so and stop - not go looking for credentials.
