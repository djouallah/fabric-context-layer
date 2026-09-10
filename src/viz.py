"""The published context -> a single self-contained graph.html.

One file, no server, no build step: open it from disk, commit it, or serve it from GitHub
Pages. The only thing it fetches is d3 from a CDN; everything else - the whole graph - is
inlined as JSON, so the page is worth about 160 KB for the current tenant.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

from common import HERE

TEMPLATE = os.path.join(HERE, "graph_template.html")


# A lakehouse table whose only edge is the one to its own lakehouse tells you nothing about
# lineage. There are hundreds of them; the page hides them behind a toggle rather than
# dropping them, so the count stays honest.
_STRUCTURAL = {"contains"}

# A DAX expression is usually a line or two; a generated one can be enormous. The page is
# a map, not an editor - past this it is truncated and the reader goes to the wiki.
EXPRESSION_LIMIT = 2000


def _clip(text, limit: int = EXPRESSION_LIMIT):
    if text is None:
        return None
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + '\n... (truncated)'


def payload(con) -> Dict:
    nodes = con.execute("SELECT id, kind, name, workspace FROM nodes").fetchall()
    edges = con.execute("SELECT src, dst, rel FROM edges").fetchall()
    terms = {t[0]: t for t in con.execute(
        "SELECT term_id, label, n_definitions, conflicting FROM terms").fetchall()}
    # The DAX behind each definition, so a measure shows what it actually computes and a
    # term shows the expression that won the ranking.
    defs = {d[0]: d for d in con.execute(
        "SELECT def_id, expression, description, rank, score, term_id, owner_item_name "
        "FROM definitions").fetchall()}
    top_def = {}
    for d in defs.values():
        if d[3] == 1:
            top_def[d[5]] = d

    ids = {n[0] for n in nodes}
    rels: Dict[str, set] = {n[0]: set() for n in nodes}
    for src, dst, rel in edges:
        rels[src].add(rel)
        if dst in ids:
            rels[dst].add(rel)

    idx: Dict[str, int] = {}
    out_nodes: List[Dict] = []
    for nid, kind, name, workspace in nodes:
        node = {"i": len(out_nodes), "k": kind, "n": name, "w": workspace or ""}
        if kind == "lakehouse_table" and rels[nid] <= _STRUCTURAL:
            node["bare"] = 1
        term = terms.get(nid.split(":", 1)[1]) if kind == "term" else None
        if term:
            node["d"] = term[2]
            if term[3]:
                node["c"] = 1
            best = top_def.get(term[0])
            if best:
                node["dax"] = _clip(best[1])
                node["own"] = best[6]         # the model the winning definition lives in
                if best[2]:
                    node["desc"] = _clip(best[2], 400)
        d = defs.get(nid)
        if d:
            node["dax"] = _clip(d[1])
            node["rk"] = d[3]
            node["sc"] = round(float(d[4] or 0), 2)
            if d[2]:
                node["desc"] = _clip(d[2], 400)
        idx[nid] = node["i"]
        out_nodes.append(node)

    out_edges, unresolved, dangling = [], {}, 0
    for src, dst, rel in edges:
        if src in idx and dst in idx:
            out_edges.append({"s": idx[src], "t": idx[dst], "r": rel})
        elif dst.startswith("unresolved:"):
            unresolved[rel] = unresolved.get(rel, 0) + 1
        else:
            dangling += 1

    return {"nodes": out_nodes, "edges": out_edges,
            "unresolved": unresolved, "dangling": dangling}


def render(con, out: str) -> str:
    with open(TEMPLATE, encoding="utf-8") as handle:
        template = handle.read()
    data = json.dumps(payload(con), separators=(",", ":"))
    # </script> inside the JSON would close the block early; < is the same string to
    # JSON.parse and inert to the HTML parser.
    data = data.replace("</", "<\\/")
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(template.replace("__DATA__", data))
    return out
