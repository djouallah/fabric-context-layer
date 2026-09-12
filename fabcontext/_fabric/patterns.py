"""Pure pattern matching over TMSL and pipeline JSON: no network, no state.

These are the only places the parsers need to know Fabric's own spellings - how a model names
the store it reads, how a partition names its table, and where a pipeline hides its nested
activities.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

# Direct Lake on OneLake names its lakehouse by a OneLake URL carrying both GUIDs.
ONELAKE_REF = re.compile(
    r"onelake\.dfs\.fabric\.microsoft\.com/([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})")

# DirectQuery on a warehouse goes through M: Sql.Database("<endpoint>", "<database>"). The
# model.bim is JSON, so in raw text those quotes may be escaped; tolerate both spellings.
SQL_DATABASE_REF = re.compile(
    r'Sql\.Database\(\\?"(?P<server>[^"\\]+\.datawarehouse\.fabric\.microsoft\.com)\\?",'
    r'\s*\\?"(?P<db>[^"\\]+)\\?"')

# A DirectQuery partition's table read, and the step the query returns. A plain read returns
# the navigation step itself; anything else transforms the table in M, and then there is no
# single physical table to bind to.
M_TABLE_READ = re.compile(
    r'(?P<step>[^\s=,]+)\s*=\s*Source\{\[Schema="(?P<schema>[^"]+)",\s*Item="(?P<item>[^"]+)"\]\}'
    r'\[Data\]')
M_RETURN = re.compile(r"\bin\s+(?P<ret>\S+)\s*$")


def m_text(expression) -> str:
    """A TMSL M expression's text. TMSL writes it either as a string or as a list of lines."""
    if isinstance(expression, list):
        return "\n".join(str(line) for line in expression)
    return "" if expression is None else str(expression)


def partition_table(part: Dict, table_name: str) -> Optional[Tuple[str, str]]:
    """The (schema, table) a partition reads - off a Direct Lake entity source, or out of a
    DirectQuery partition's M table read. None when it is not a plain table read."""
    src = part.get("source") or {}
    if src.get("type") == "entity" or src.get("entityName"):
        return src.get("schemaName") or "dbo", src.get("entityName") or table_name
    text = m_text(src.get("expression"))
    read, returned = M_TABLE_READ.search(text), M_RETURN.search(text.strip())
    if read and returned and returned.group("ret") == read.group("step"):
        return read.group("schema"), read.group("item")
    return None


def walk_activities(activities):
    """Every activity, recursing into the containers. ForEach and Until nest under
    `typeProperties.activities`, If under `ifTrueActivities` and `ifFalseActivities`, Switch
    under `cases[].activities` and `defaultActivities` - so a notebook activity is found
    wherever it sits, not only at the top level."""
    for act in activities or []:
        yield act
        props = act.get("typeProperties") or {}
        for key in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            yield from walk_activities(props.get(key))
        for case in props.get("cases") or []:
            yield from walk_activities(case.get("activities"))
