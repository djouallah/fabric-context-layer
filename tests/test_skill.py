"""The skill, against the contracts the agents discover and run it by.

One protocol, one file: `client/SKILL.md`. It is installed under `~/.claude/skills/`, a
workspace's `.claude/skills/`, or `~/.copilot/skills/` for Scout, and it drives the Power BI
API directly - so it must be self-contained: the token, the ids, the endpoint, all in the
file. `.claude/skills/fabric-context/SKILL.md` is a copy of it, which is how Claude Code and
GitHub Copilot in this clone follow the same protocol as everyone else.

A renamed directory or a stray capital in `name` raises nothing; the skill silently stops
being offered, and tenant questions get answered from memory.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, ".claude", "skills", "fabric-context")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
CLIENT_SKILL = os.path.join(ROOT, "client", "SKILL.md")
INSTRUCTIONS = os.path.join(ROOT, ".github", "copilot-instructions.md")

_NAME = re.compile(r"^[a-z0-9-]+$")


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


def test_the_skill_carries_the_frontmatter_the_agents_discover_by():
    fields = _frontmatter(CLIENT_SKILL)
    name = fields.get("name", "")
    assert _NAME.match(name), (
        "a skill name is lowercase letters, digits and hyphens; " + repr(name)
        + " is not offered by any Copilot surface")
    assert name == "fabric-context", (
        "the name must equal the directory it is installed into, and that is fabric-context")
    assert fields.get("description"), "the description is how an agent decides when to load it"
    assert os.path.basename(SKILL_DIR) == "fabric-context"


def test_the_clone_carries_the_same_skill_and_not_a_second_protocol():
    """One client protocol. The copy under .claude/skills/ is how this clone's Claude Code and
    Copilot follow it; a copy that drifts is a second protocol nobody reviews."""
    assert _read(SKILL) == _read(CLIENT_SKILL), (
        ".claude/skills/fabric-context/SKILL.md must be identical to client/SKILL.md - "
        "copy it over, never edit the copy")


def test_the_skill_says_what_its_one_call_needs():
    """It is copied out of the repo to a machine that has no clone and no CLAUDE.md, so
    everything it needs - the token, the ids, the endpoint - has to be in the file."""
    text = _read(CLIENT_SKILL)
    for need in ("az login", "FABRIC_CONTEXT_MODEL", "executeQueries",
                 "https://analysis.windows.net/powerbi/api"):
        assert need in text, ("client/SKILL.md must carry its own setup - " + need + " - "
                              "because it is read on a machine with nothing else beside it")


def test_the_skill_looks_for_the_names_the_harvest_publishes_under():
    """The skill finds the context by name, and nothing hands it the names.

    It lists the tenant's workspaces, takes the one called `context_layer` and the semantic
    model called `context_model` inside it. Both spellings are literals in a markdown file on
    a machine with no clone, so renaming either one here silently leaves that file looking for
    something the harvest no longer writes - and the failure lands on the asking side, as "the
    context layer has not been published in this tenant".
    """
    import fabcontext
    from fabcontext import semantic_model

    text = _read(CLIENT_SKILL)
    for name in (fabcontext.DEFAULT_WORKSPACE, semantic_model.DEFAULT_NAME):
        assert name in text, (
            "client/SKILL.md must name " + repr(name) + " literally - it is how the skill "
            "finds the context, and it has nothing else to read")


def test_copilot_instructions_route_to_the_skill():
    assert "fabric-context" in _read(INSTRUCTIONS), (
        "the instructions must name the skill they route to")
