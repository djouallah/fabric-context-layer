"""The M365 Copilot agent's instructions, against the two things that can silently break them.

The agent is built in a portal, so nothing here can test the agent itself. What it can test is
the file that is pasted into it: that it still fits the surface's hard limit, and that every
column it asks for is still a column the harvest publishes. Both fail silently in production -
an over-long instruction set is truncated mid-protocol, and a renamed column returns an empty
table that reads as "the term is not defined" rather than as an error.
"""
from __future__ import annotations

import os
import re

from tests.test_publish import OPTIONAL, REQUIRED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUCTIONS = os.path.join(ROOT, "copilot", "instructions.md")

# Copilot Studio and declarative agents both cap instructions at 8,000 characters. Leave room:
# community reports say the combined system text can trip an internal ceiling sooner.
LIMIT = 8000
HEADROOM = 0.85

_REF = re.compile(r"'(terms|definitions|aliases)'\[(\w+)\]")


def _body() -> str:
    """What actually gets pasted: everything after the `---` separator."""
    with open(INSTRUCTIONS, encoding="utf-8") as handle:
        text = handle.read()
    _, sep, body = text.partition("\n---\n")
    assert sep, "instructions.md must separate its preamble from the pasted body with ---"
    return body


def test_instructions_fit_the_instruction_limit():
    body = _body()
    assert len(body) <= LIMIT * HEADROOM, (
        str(len(body)) + " chars; the surface caps instructions at " + str(LIMIT)
        + " and the design only works because the ranking lives in the model, not in here")


def test_every_column_the_agent_queries_is_published():
    published = {table: set(cols) for table, cols in REQUIRED.items()}
    for table, cols in OPTIONAL.items():
        published.setdefault(table, set()).update(cols)
    missing = sorted({(t, c) for t, c in _REF.findall(_body()) if c not in published.get(t, ())})
    assert not missing, ("the instructions query columns the harvest does not publish: "
                         + str(missing) + " - a renamed column returns an empty table, which "
                         "the agent reads as 'no definition' rather than as an error")


def test_the_agent_is_told_which_ids_to_run_the_measure_on():
    body = _body()
    for column in ("workspace_id", "owner_item_id"):
        assert "'definitions'[" + column + "]" in body, (
            "the lookup must return " + column + "; without both GUIDs the agent cannot run "
            "the measure on the model that owns it")
