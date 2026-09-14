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
```

PowerShell:

```powershell
$dir = "$HOME\.claude\skills\fabric-context"; mkdir $dir -Force
iwr https://raw.githubusercontent.com/djouallah/fabric-context-layer/main/agent/SKILL.md -OutFile $dir\SKILL.md

az login
```

Skills are discovered when a session starts, so open a new one. Then ask a question in plain
English - "what was revenue last quarter" - and the skill does the rest.

No ids to configure: the skill finds `context_model` in the workspace named `context_layer`.
Setting `FABRIC_CONTEXT_MODEL` to `<workspace-guid>/<model-guid>` skips that lookup, and is
the way to point it at a context published somewhere else.

## Inside this repo

A clone of `fabric-context-layer` ships its own `.claude/skills/fabric-context/`, which is
for **working on the code**, and a project skill wins over a personal one. This one is for
**asking a question** with nothing installed. They are different jobs, and outside the clone
only this one is loaded.
