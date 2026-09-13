"""The one skill, against the contracts the other agents discover and run it by.

`.claude/skills/fabric-context/SKILL.md` is written for Claude Code, but GitHub Copilot - in
VS Code, the CLI and the cloud agent - loads Agent Skills from `.claude/skills/` too, and
Microsoft Scout loads the same folder once it is linked under `~/.copilot/skills/`. Both
decide by the frontmatter alone: a lowercase-hyphen `name` and a `description` saying when to
use it. A renamed directory or a stray capital raises nothing; the skill silently stops being
offered, and tenant questions get answered from memory.

Scout has no other channel - no CLAUDE.md, no copilot-instructions.md beside the skill - so
the skill must say itself what its commands need before the first one runs. And the one thing
that must never land in `.github/copilot-instructions.md` is the other Copilot's protocol: the
M365 agent's DAX lookup over the context model.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, ".claude", "skills", "fabric-context")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
INSTRUCTIONS = os.path.join(ROOT, ".github", "copilot-instructions.md")

_NAME = re.compile(r"^[a-z0-9-]+$")
# The M365 agent's lookup: DAX over the context model's own tables (copilot/instructions.md).
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


def test_skill_frontmatter_is_what_the_agents_discover_by():
    fields = _frontmatter(SKILL)
    name = fields.get("name", "")
    assert _NAME.match(name), ("a skill name is lowercase letters, digits and hyphens; "
                               + repr(name) + " is not offered by any Copilot surface")
    assert name == os.path.basename(SKILL_DIR), "the skill name must equal its directory name"
    assert fields.get("description"), "the description is how an agent decides when to load it"


def test_skill_says_what_its_commands_need():
    text = _read(SKILL)
    for need in ("pip install -e .", "az login", "--db"):
        assert need in text, (
            "the skill must carry its own setup - " + need + " - because Scout reads it from "
            "~/.copilot/skills with no CLAUDE.md or copilot-instructions.md beside it")


def test_copilot_instructions_route_to_the_skill_and_not_to_the_m365_protocol():
    text = _read(INSTRUCTIONS)
    assert "fabric-context" in text, "the instructions must name the skill they route to"
    assert not _M365_REF.search(text), (
        "these instructions are for GitHub Copilot, which runs python -m ask; the DAX lookup "
        "over 'terms'/'definitions'/'aliases' belongs to the M365 agent in copilot/")
