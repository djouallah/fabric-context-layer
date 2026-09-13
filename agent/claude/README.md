# Claude Code and Claude Desktop

Claude Code discovers skills in `~/.claude/skills/` (yours, every directory) and in a
project's `.claude/skills/` (that project only). Put it in the first, so a question can be
asked from anywhere.

## Setup

```bash
mkdir -p ~/.claude/skills/fabric-context
curl -fsSL -o ~/.claude/skills/fabric-context/SKILL.md \
  https://raw.githubusercontent.com/djouallah/fabric-context-layer/main/agent/SKILL.md

az login
export FABRIC_CONTEXT_MODEL="<workspace-guid>/<model-guid>"   # from context_model's address
```

PowerShell:

```powershell
$dir = "$HOME\.claude\skills\fabric-context"; mkdir $dir -Force
iwr https://raw.githubusercontent.com/djouallah/fabric-context-layer/main/agent/SKILL.md -OutFile $dir\SKILL.md

az login
setx FABRIC_CONTEXT_MODEL "<workspace-guid>/<model-guid>"
```

Skills are discovered when a session starts, so open a new one. Then ask a question in plain
English - "what was revenue last quarter" - and the skill does the rest.

`FABRIC_CONTEXT_MODEL` is a convenience. Without it the skill asks for the `context_model`
link the first time it needs one, and remembers it for that conversation.

## Inside this repo

A clone of `fabric-context-layer` ships its own `.claude/skills/fabric-context/`, and a
project skill wins over a personal one. That is intended: the repo's copy is for **working on
the code**, and it reads the whole graph out of `context.md` through the clone's query side.
This one is for **asking a question** with nothing installed. They are different jobs, and
outside the clone only this one is loaded.
