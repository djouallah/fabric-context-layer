"""The two instruction files in `agent/`, against what can silently break them.

Neither is executed by anything here - one is pasted into a portal, the other is dropped into
a skills folder on someone else's machine - so what can be tested is the text. Three things
fail silently in production:

- an over-long instruction set is truncated mid-protocol (Copilot Studio's hard cap);
- a renamed published column returns an *empty table*, which an agent reads as "the term is
  not defined" rather than as an error;
- a setup step that reaches for the clone or the harvest puts the whole repo back in front of
  someone who only wanted a number, which is the thing `agent/` exists to prevent.
"""
from __future__ import annotations

import os
import re

from tests.test_publish import OPTIONAL, REQUIRED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent")
M365 = os.path.join(AGENT, "m365", "instructions.md")
SKILL = os.path.join(AGENT, "SKILL.md")

# Copilot Studio and declarative agents both cap instructions at 8,000 characters. Leave room:
# community reports say the combined system text can trip an internal ceiling sooner.
LIMIT = 8000
HEADROOM = 0.85

_REF = re.compile(r"'(terms|definitions|aliases)'\[(\w+)\]")

# What the install-free side must never ask of the person asking a question. `agent/` is the
# answering side; the context already exists.
_CLONE = ("git clone", "pip install", "python -m ask", "python -m fabcontext",
          "fabcontext.harvest")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _pasted(path: str) -> str:
    """What actually gets pasted into the portal: everything after the `---` separator."""
    _, sep, body = _read(path).partition("\n---\n")
    assert sep, path + " must separate its preamble from the pasted body with ---"
    return body


def test_m365_instructions_fit_the_instruction_limit():
    body = _pasted(M365)
    assert len(body) <= LIMIT * HEADROOM, (
        str(len(body)) + " chars; the surface caps instructions at " + str(LIMIT)
        + " and the design only works because the ranking lives in the model, not in here")


def test_every_column_either_file_queries_is_published():
    """Both protocols read the same three published tables, so both are held to the schema."""
    published = {table: set(cols) for table, cols in REQUIRED.items()}
    for table, cols in OPTIONAL.items():
        published.setdefault(table, set()).update(cols)
    for path in (M365, SKILL):
        missing = sorted({(t, c) for t, c in _REF.findall(_read(path))
                          if c not in published.get(t, ())})
        assert not missing, (
            os.path.relpath(path, ROOT) + " queries columns the harvest does not publish: "
            + str(missing) + " - a renamed column returns an empty table, which the agent "
            "reads as 'no definition' rather than as an error")


def test_both_are_told_which_ids_to_run_the_measure_on():
    for path in (M365, SKILL):
        body = _read(path)
        for column in ("workspace_id", "owner_item_id"):
            assert "'definitions'[" + column + "]" in body, (
                os.path.relpath(path, ROOT) + " must return " + column + "; without both "
                "GUIDs the agent cannot run the measure on the model that owns it")


def test_the_install_free_side_never_reaches_for_the_clone():
    """The point of `agent/`: answering a question costs a Power BI connection and two ids.

    Any of these creeping into the skill or a setup prompt puts the harvest repo back in the
    way, which is exactly the friction this folder replaced.
    """
    for name in ("SKILL.md", os.path.join("scout", "README.md"),
                 os.path.join("claude", "README.md"), os.path.join("copilot", "README.md")):
        path = os.path.join(AGENT, name)
        body = _read(path).lower()
        # The skill's "Never" section names them in order to forbid them; that is the one
        # place the words belong, so only the instructions above it are checked.
        body = body.split("## never")[0]
        hits = sorted(phrase for phrase in _CLONE if phrase in body)
        assert not hits, (os.path.join("agent", name) + " tells the reader to " + str(hits)
                          + " - asking a question needs a Power BI connection and the "
                          "context model's ids, and nothing else")
