"""The two skill files, against the contracts the agents discover and run them by.

There are two, for two different situations, and each is discovered by its frontmatter alone:

- `.claude/skills/fabric-context/SKILL.md` - **in this clone**. Claude Code loads it, and
  GitHub Copilot loads Agent Skills from `.claude/skills/` too. It drives `python -m ask`, so
  it must carry what those commands need before the first one runs.
- `agent/SKILL.md` - **everywhere else**. The same file is installed under
  `~/.claude/skills/`, a workspace's `.claude/skills/`, or `~/.copilot/skills/` for Scout, and
  it drives the Power BI API directly. Scout has no other channel - no CLAUDE.md, no
  copilot-instructions.md beside it - so it too must be self-contained.

A renamed directory or a stray capital in `name` raises nothing; the skill silently stops
being offered, and tenant questions get answered from memory.

The one thing that must never land in `.github/copilot-instructions.md` is the other Copilot's
protocol: the M365 agent's DAX lookup over the context model.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, ".claude", "skills", "fabric-context")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
AGENT_SKILL = os.path.join(ROOT, "agent", "SKILL.md")
INSTRUCTIONS = os.path.join(ROOT, ".github", "copilot-instructions.md")

_NAME = re.compile(r"^[a-z0-9-]+$")
# The M365 agent's lookup: DAX over the context model's own tables (agent/m365/instructions.md).
_M365_REF = re.compile(r"'(terms|definitions|aliases)'\[")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _frontmatter(path):
    text = _read(path)
    assert text.startswith("---\n"), path + " must open with a YAML frontmatter block"
    block, sep, _ = text[4:].partition("\n---\n")
    assert sep, path + " frontmatter is not closed"
    fields = {}
    for line in block.splitlines():
        key, colon, value = line.partition(":")
        if colon and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    return fields


def test_both_skills_carry_the_frontmatter_the_agents_discover_by():
    for path in (SKILL, AGENT_SKILL):
        fields = _frontmatter(path)
        name = fields.get("name", "")
        assert _NAME.match(name), (
            os.path.relpath(path, ROOT) + ": a skill name is lowercase letters, digits and "
            "hyphens; " + repr(name) + " is not offered by any Copilot surface")
        assert name == "fabric-context", (
            os.path.relpath(path, ROOT) + ": the name must equal the directory it is "
            "installed into, and both are installed as fabric-context")
        assert fields.get("description"), (
            os.path.relpath(path, ROOT) + ": the description is how an agent decides when to "
            "load it")
    assert os.path.basename(SKILL_DIR) == "fabric-context"


def test_the_clone_skill_says_what_its_commands_need():
    """It drives `python -m ask` from this clone, and nothing beside it says so."""
    text = _read(SKILL)
    for need in ("pip install -e .", "az login", "--db"):
        assert need in text, "the clone's skill must carry its own setup - " + need


def test_the_installed_skill_says_what_its_one_call_needs():
    """It is copied out of the repo to a machine that has no clone and no CLAUDE.md, so
    everything it needs - the token, the ids, the endpoint - has to be in the file."""
    text = _read(AGENT_SKILL)
    for need in ("az login", "FABRIC_CONTEXT_MODEL", "executeQueries",
                 "https://analysis.windows.net/powerbi/api"):
        assert need in text, ("agent/SKILL.md must carry its own setup - " + need + " - "
                              "because it is read on a machine with nothing else beside it")


def test_copilot_instructions_route_to_the_skill_and_not_to_the_m365_protocol():
    text = _read(INSTRUCTIONS)
    assert "fabric-context" in text, "the instructions must name the skill they route to"
    assert not _M365_REF.search(text), (
        "these instructions are for GitHub Copilot in this clone, which runs python -m ask; "
        "the DAX lookup over 'terms'/'definitions'/'aliases' belongs to the M365 agent in "
        "agent/m365/")
