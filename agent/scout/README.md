# Microsoft Scout

Scout is the desktop agent. It runs a shell, so it can drive the Azure CLI itself; it loads
skills from `~/.copilot/skills/`, not from a repo, and it reads no instruction file. So the
setup is one paste, and Scout does the rest.

## 1. Paste this into a Scout conversation

```
Set up the fabric-context skill on this machine, then use it once.

1. Create ~/.copilot/skills/fabric-context/ and download
   https://raw.githubusercontent.com/djouallah/fabric-context-layer/main/agent/SKILL.md
   into it as SKILL.md.
2. Run `az account show`. If it fails, tell me to run `az login` and stop here.
3. Ask me for the Fabric address of the semantic model named context_model, read the two
   GUIDs out of it, and set FABRIC_CONTEXT_MODEL for my user to
   <workspace-guid>/<model-guid>.
4. Read the skill you just downloaded, run its step 1 lookup against context_model, and
   tell me how many terms the layer holds and which of them are conflicting.
```

Scout asks before its first network command; allow it. Nothing is downloaded but the skill
itself, and nothing is installed.

## 2. Start a new conversation and ask it something

Skills are discovered when a conversation starts, so the one just written is loaded from the
next one on. Ask:

```
What was revenue last quarter?
```

Scout asks `context_model` which definition of the term wins, reads the owning model's two
GUIDs off that row, runs the measure by name against it, and answers with the number first,
then its sources, then a confidence. The protocol it follows is [the skill](../SKILL.md);
nothing was pasted into Scout but the setup above.

**Ask something you already know the answer to first**, and check it by running the same
query by hand - because a wrong number looks exactly like a right one: the agent still writes
the final DAX, and a mistaken filter returns a plausible figure with no error at all.
