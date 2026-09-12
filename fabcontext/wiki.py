"""Step 4: the published context -> wiki/, a folder of linked markdown pages, and
`context.md`, the same thing as one file.

The graph is the source of truth; this is the readable projection of it. One page per item
and one page per business term, wikilinked both ways, so a person in Obsidian and Claude
Code in a terminal navigate the same thing. Pages stay short on purpose - the detail lives
in the database and the page says where to look.

`context.md` is that projection for a reader who cannot open the database: one file, fixed
headings, no links to follow, so an agent greps a section instead of downloading twelve
Delta tables first. It holds no numbers either - a number comes from DAX on a model,
and the file carries the ids that call takes.
"""
from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .common import page_slug, term_label, write_text

# Kinds that get their own page, and the folder each lands in.
PAGE_FOLDER = {
    "term": "terms", "semantic_model": "models", "report": "reports",
    "dashboard": "dashboards", "notebook": "notebooks", "pipeline": "pipelines",
    "dataflow": "dataflows", "lakehouse": "stores", "warehouse": "stores",
    "lakehouse_table": "tables", "workspace": "workspaces",
}
# Sub-objects that link to their parent page with a heading anchor.
ANCHORED = {"measure": True, "report_measure": True, "column": False, "page": False,
            "visual": False}

PORTAL = "https://app.fabric.microsoft.com/groups/{ws}/{kind}/{id}"
PORTAL_KIND = {"report": "reports", "semantic_model": "datasets", "dashboard": "dashboards",
               "notebook": "synapsenotebooks", "pipeline": "pipelines",
               "lakehouse": "lakehouses", "warehouse": "warehouses",
               "dataflow": "dataflows-gen2"}


class Wiki:
    def __init__(self, con, out_dir: str):
        self.con = con
        self.out = out_dir
        self.nodes: Dict[str, dict] = {}
        self.slug: Dict[str, str] = {}
        self.out_edges: Dict[str, List[tuple]] = defaultdict(list)
        self.in_edges: Dict[str, List[tuple]] = defaultdict(list)
        self.views: Dict[str, int] = {}
        self.labels: Dict[str, str] = {}
        self.written: List[str] = []
        self._load()

    # ---------------------------------------------------------------- loading

    def _load(self) -> None:
        cols = ("id, kind, name, workspace, item_id, parent_id, description, endorsement, "
                "owner, modified_at, attrs, tier")
        for row in self.con.execute("SELECT " + cols + " FROM nodes").fetchall():
            node = dict(zip(cols.replace(" ", "").split(","), row))
            self.nodes[node["id"]] = node
        for src, dst, rel, weight, attrs in self.con.execute(
                "SELECT src, dst, rel, weight, attrs FROM edges").fetchall():
            self.out_edges[src].append((dst, rel, weight, attrs))
            self.in_edges[dst].append((src, rel, weight, attrs))
        for item_id, views in self.con.execute(
                "SELECT item_id, views FROM item_views").fetchall():
            self.views[item_id] = views
        for tid, label in self.con.execute("SELECT term_id, label FROM terms").fetchall():
            self.labels[tid] = label
        for nid, node in self.nodes.items():
            if node["kind"] == "term":
                self.slug[nid] = nid.split(":", 1)[1]
            elif node["kind"] in PAGE_FOLDER and not self.is_tail(nid):
                key = node.get("item_id") or nid
                self.slug[nid] = (self._table_slug(node)
                                  if node["kind"] == "lakehouse_table"
                                  else page_slug(node["name"], key))

    def is_tail(self, nid: str) -> bool:
        """Tier 3: harvested, but nothing in the tenant refers to it (see graph._tier).

        It gets no page of its own. Giving one to every table in a sandbox lakehouse
        buries the handful that a model is actually built on. It is still listed on its
        parent's page, still in the database, still reachable from `ask`."""
        return (self.nodes.get(nid, {}).get("tier") or 1) >= 3

    def term_name(self, tid: str) -> str:
        """The top-ranked measure's own name, falling back to a title-cased id."""
        return self.labels.get(tid) or term_label(tid)

    def _table_slug(self, node: dict) -> str:
        store = self._attr(node, "store") or "store"
        schema = self._attr(node, "schema") or "dbo"
        return page_slug(store, node.get("item_id") or "") + "." + schema + "." + node["name"]

    @staticmethod
    def _attr(node: dict, key: str, default=None):
        import json
        raw = node.get("attrs")
        if not raw:
            return default
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return default
        return raw.get(key, default) if isinstance(raw, dict) else default

    # ---------------------------------------------------------------- links

    def link(self, nid: str, label: Optional[str] = None) -> str:
        node = self.nodes.get(nid)
        if not node:
            if nid.startswith("unresolved:"):
                return "`" + nid.split("/", 1)[-1] + "` (unresolved)"
            return "`" + nid + "`"
        text = label or node["name"]
        if nid in self.slug:
            return "[[" + self.slug[nid] + "|" + text + "]]"
        parent = node.get("parent_id")
        anchored = ANCHORED.get(node["kind"])
        if anchored and parent:
            owner = self._page_ancestor(nid)
            if owner:
                return "[[" + self.slug[owner] + "#" + node["name"] + "|" + text + "]]"
        owner = self._page_ancestor(nid)
        if owner:
            return "[[" + self.slug[owner] + "|" + text + "]]"
        return "`" + text + "`"

    def _page_ancestor(self, nid: str) -> Optional[str]:
        seen = set()
        cur = self.nodes.get(nid, {}).get("parent_id")
        while cur and cur not in seen:
            seen.add(cur)
            if cur in self.slug:
                return cur
            cur = self.nodes.get(cur, {}).get("parent_id")
        return None

    # ---------------------------------------------------------------- writing

    def write(self, kind: str, slug: str, front: dict, body: List[str]) -> None:
        lines = ["---"]
        for key, value in front.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                lines.append(key + ": [" + ", ".join('"' + str(v) + '"' for v in value) + "]")
            elif isinstance(value, bool):
                lines.append(key + ": " + ("true" if value else "false"))
            elif isinstance(value, (int, float)):
                lines.append(key + ": " + str(value))
            else:
                text = str(value).replace("\n", " ")
                lines.append(key + ': "' + text.replace('"', "'") + '"')
        lines.append("---")
        lines.extend(body)
        path = os.path.join(self.out, PAGE_FOLDER[kind], slug + ".md")
        write_text(path, "\n".join(lines).rstrip() + "\n")
        self.written.append(path)

    def portal_url(self, node: dict) -> Optional[str]:
        kind = PORTAL_KIND.get(node["kind"])
        ws_id = self._workspace_id(node)
        if kind and ws_id and node.get("item_id"):
            return PORTAL.format(ws=ws_id, kind=kind, id=node["item_id"])
        return None

    def _workspace_id(self, node: dict) -> Optional[str]:
        for nid, other in self.nodes.items():
            if other["kind"] == "workspace" and other["name"] == node.get("workspace"):
                return other.get("item_id")
        return None


# ---------------------------------------------------------------- page builders

def render(con, out_dir: str, context_md: Optional[str] = None) -> Dict[str, int]:
    """Write `out_dir` as a wiki, and - when `context_md` names a path - the same context as
    one markdown file. It sits beside the folder rather than inside it: the folder is rebuilt
    from scratch on every run, and this file has no wikilinks to keep consistent with it."""
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    w = Wiki(con, out_dir)
    counts: Dict[str, int] = defaultdict(int)

    for term_id, label, n_defs, n_distinct, n_items, conflicting, top, views, n_reports in \
            con.execute("SELECT term_id, label, n_definitions, n_distinct_expr, n_items, "
                        "conflicting, top_def_id, views, n_reports FROM terms "
                        "ORDER BY views DESC").fetchall():
        _term_page(w, con, term_id, label or term_label(term_id), n_defs, n_distinct,
                   conflicting, views)
        counts["term"] += 1

    for nid, node in sorted(w.nodes.items()):
        kind = node["kind"]
        if kind in PAGE_FOLDER and w.is_tail(nid):
            counts["tail"] += 1               # listed on its parent's page, not its own
            continue
        if kind == "semantic_model":
            _model_page(w, con, nid, node)
        elif kind == "report":
            _report_page(w, nid, node)
        elif kind in ("lakehouse", "warehouse"):
            _store_page(w, nid, node)
        elif kind == "lakehouse_table":
            _table_page(w, nid, node)
        elif kind == "notebook":
            _notebook_page(w, nid, node)
        elif kind == "pipeline":
            _pipeline_page(w, nid, node)
        elif kind == "dashboard":
            _simple_page(w, nid, node, "dashboard")
        elif kind == "dataflow":
            _simple_page(w, nid, node, "dataflow")
        elif kind == "workspace":
            _workspace_page(w, nid, node)
        else:
            continue
        counts[kind] += 1

    _index_page(w, con)
    _claude_page(w, con)
    if context_md:
        counts["context_md"] = _context_page(w, con, context_md)
    return dict(counts)


def _term_page(w: Wiki, con, term_id: str, label: str, n_defs: int, n_distinct: int,
               conflicting: bool, views: int) -> None:
    defs = con.execute(
        "SELECT def_id, rank, name, kind, owner_item_name, table_name, endorsement, "
        "       views, n_reports, n_visuals, score, expression, description, workspace "
        "  FROM definitions WHERE term_id = ? ORDER BY rank", [term_id]).fetchall()
    if not defs:
        return
    top = defs[0]
    body: List[str] = ["# " + label, ""]

    verdict = (str(n_defs) + " definition" + ("s" if n_defs != 1 else "")
               + " across " + str(len({d[4] for d in defs})) + " item"
               + ("s" if len({d[4] for d in defs}) != 1 else "") + ". ")
    verdict += ("Definitions disagree - " + str(n_distinct) + " different expressions."
                if conflicting else "All definitions agree.")
    body += [verdict, "",
             "Ranked first: " + w.link(top[0], top[2]) + " in " + str(top[4])
             + (" (" + str(top[6]).lower() + ")" if top[6] else "")
             + ", " + str(top[7]) + " views in the last 28 days.", ""]

    aliases = [r[0] for r in con.execute(
        "SELECT DISTINCT alias FROM aliases WHERE term_id = ? ORDER BY 1", [term_id]).fetchall()]
    if len(aliases) > 1:
        body += ["Also known as: " + ", ".join(aliases) + ".", ""]

    body += ["## Ranked definitions", "",
             "| # | measure | in | endorsement | views | reports | score |",
             "|---|---------|----|-------------|-------|---------|-------|"]
    for d in defs:
        body.append("| " + str(d[1]) + " | " + w.link(d[0], d[2]) + " | " + str(d[4])
                    + " | " + (str(d[6]) if d[6] else "-") + " | " + str(d[7])
                    + " | " + str(d[8]) + " | " + str(round(d[10], 2)) + " |")
    body.append("")

    body += ["## Expressions", ""]
    for d in defs:
        body.append("**#" + str(d[1]) + " " + d[2] + "** in " + str(d[4])
                    + (" - " + d[12] if d[12] else ""))
        body += ["", "```dax", (d[11] or "(no expression captured)").strip(), "```", ""]

    reports = con.execute(
        "SELECT DISTINCT r.id, r.name, coalesce(iv.views, 0) AS views, r.workspace "
        "  FROM definitions d "
        "  JOIN measure_usage mu ON mu.def_id = d.def_id "
        "  JOIN nodes r ON r.id = 'report:' || mu.report_id "
        "  LEFT JOIN item_views iv ON iv.item_id = mu.report_id "
        " WHERE d.term_id = ? ORDER BY views DESC", [term_id]).fetchall()
    if reports:
        body += ["## Used in reports", "",
                 "| report | workspace | views (28d) |", "|--------|-----------|-------|"]
        body += ["| " + w.link(r[0], r[1]) + " | " + str(r[3]) + " | " + str(r[2]) + " |"
                 for r in reports]
        body.append("")

    upstream = _upstream_of(w, con, [d[0] for d in defs])
    if upstream:
        body += ["## Upstream", ""]
        body += ["- " + w.link(nid) + " (" + kind.replace("_", " ") + ")"
                 for nid, kind in upstream]
        body.append("")

    mentions = [src for src, rel, _weight, _a in w.in_edges.get("term:" + term_id, [])
                if rel == "mentions"]
    if mentions:
        body += ["## Mentioned in", ""] + ["- " + w.link(m) for m in mentions] + [""]

    w.write("term", term_id, {
        "id": "term:" + term_id, "kind": "term", "name": label,
        "aliases": (aliases or sorted({d[2] for d in defs}))[:12],
        "tags": ["term"] + (["conflict"] if conflicting else []),
        "definitions": n_defs, "conflicting": bool(conflicting),
        "views_28d": views, "top": top[2],
    }, body)


def _upstream_of(w: Wiki, con, def_ids: List[str], max_depth: int = 6):
    """The physical tables, notebooks and pipelines behind a term's definitions."""
    from .graph import lineage
    seen: Dict[str, str] = {}
    for def_id in def_ids[:5]:
        for nid, kind, _name, _ws, _depth, _rel in lineage(con, def_id, "up", max_depth):
            if kind in ("lakehouse_table", "notebook", "pipeline", "dataflow", "datasource",
                        "lakehouse", "warehouse"):
                seen.setdefault(nid, kind)
    return sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))[:25]


def _model_page(w: Wiki, con, nid: str, node: dict) -> None:
    body: List[str] = ["# " + node["name"], ""]
    if node.get("description"):
        body += [node["description"], ""]
    tables = [(dst, w.nodes[dst]) for dst, rel, _wt, _a in w.out_edges.get(nid, [])
              if rel == "contains" and dst in w.nodes and w.nodes[dst]["kind"] == "model_table"]

    sources = []
    for tid, _t in tables:
        for dst, rel, _wt, _a in w.out_edges.get(tid, []):
            if rel == "sources_from":
                sources.append((tid, dst))
    if tables:
        body += ["## Tables", "", "| table | reads | columns | measures |",
                 "|-------|-------|---------|----------|"]
        for tid, table in sorted(tables, key=lambda t: t[1]["name"]):
            reads = [w.link(dst) for src, dst in sources if src == tid] or ["-"]
            body.append("| " + table["name"] + " | " + ", ".join(reads) + " | "
                        + str(Wiki._attr(table, "n_columns", 0)) + " | "
                        + str(Wiki._attr(table, "n_measures", 0)) + " |")
        body.append("")

    measures = con.execute(
        "SELECT d.def_id, d.name, d.table_name, d.expression, d.description, d.term_id, "
        "       d.rank, d.conflicting FROM definitions d WHERE d.owner_item_id = ? "
        " ORDER BY d.table_name, d.name", [node.get("item_id")]).fetchall()
    if measures:
        body += ["## Measures", ""]
        for def_id, name, table, expr, desc, term_id, rank, conflicting in measures:
            body.append("### " + name)
            body.append("")
            meta = ["table: " + str(table),
                    "defines [[" + term_id + "|" + w.term_name(term_id) + "]] (ranked #"
                    + str(rank) + ")"]
            if conflicting:
                meta.append("**other definitions of this term disagree**")
            body += ["- " + m for m in meta]
            if desc:
                body += ["", desc]
            body += ["", "```dax", (expr or "").strip(), "```", ""]

    reports = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "uses"]
    if reports:
        body += ["## Used by", ""] + ["- " + w.link(r) for r in sorted(set(reports))] + [""]

    w.write("semantic_model", w.slug[nid], _front(w, nid, node, {
        "aliases": [m[1] for m in measures][:20],
        "storage_mode": Wiki._attr(node, "storage_mode"),
    }), body)


def _report_page(w: Wiki, nid: str, node: dict) -> None:
    body: List[str] = ["# " + node["name"], ""]
    if node.get("description"):
        body += [node["description"], ""]
    model = [dst for dst, rel, _wt, _a in w.out_edges.get(nid, []) if rel == "uses"]
    if model:
        body += ["Reads " + ", ".join(w.link(m) for m in model) + ".", ""]

    pages = [(dst, w.nodes[dst]) for dst, rel, _wt, _a in w.out_edges.get(nid, [])
             if rel == "contains" and dst in w.nodes and w.nodes[dst]["kind"] == "page"]
    if pages:
        body += ["## Pages", "", "| page | visuals |", "|------|---------|"]
        body += ["| " + p["name"] + " | " + str(Wiki._attr(p, "n_visuals", 0)) + " |"
                 for _pid, p in sorted(pages, key=lambda x: x[1]["name"])]
        body.append("")

    fields: Dict[str, int] = {}
    for pid, _p in pages:
        for vid, rel, _wt, _a in w.out_edges.get(pid, []):
            if rel != "contains":
                continue
            for dst, vrel, weight, _at in w.out_edges.get(vid, []):
                if vrel == "references":
                    fields[dst] = fields.get(dst, 0) + int(weight or 1)
    if fields:
        body += ["## Fields used", "", "| field | visuals |", "|-------|---------|"]
        body += ["| " + w.link(f) + " | " + str(c) + " |"
                 for f, c in sorted(fields.items(), key=lambda kv: -kv[1])[:60]]
        body.append("")

    local = [dst for dst, rel, _wt, _a in w.out_edges.get(nid, [])
             if rel == "contains" and dst in w.nodes
             and w.nodes[dst]["kind"] == "report_measure"]
    if local:
        body += ["## Measures defined in this report", ""]
        for mid in local:
            m = w.nodes[mid]
            body += ["### " + m["name"], "", "```dax",
                     str(Wiki._attr(m, "expression", "")).strip(), "```", ""]

    w.write("report", w.slug[nid], _front(w, nid, node, {}), body)


def _store_page(w: Wiki, nid: str, node: dict) -> None:
    tables = [dst for dst, rel, _wt, _a in w.out_edges.get(nid, []) if rel == "contains"]
    body = ["# " + node["name"], "",
            str(len(tables)) + " tables.", ""]
    if Wiki._attr(node, "external"):
        body += ["This store was not harvested: something in a harvested workspace binds to "
                 "it (" + str(Wiki._attr(node, "via", "reference")) + "), so it appears here "
                 "as a stub. Harvest its workspace to fill it in.", ""]
    by_name = sorted(tables, key=lambda t: w.nodes.get(t, {}).get("name", ""))
    used = [t for t in by_name if not w.is_tail(t)]
    tail = [t for t in by_name if w.is_tail(t)]
    if used:
        body += ["## Tables", ""] + ["- " + w.link(t) for t in used] + [""]
    if tail:
        # No page each: nothing in the harvested workspaces reads them, so a page would
        # say only that. Named here so the store's inventory is still complete, and
        # still in the database, and named under its store in `context.md`.
        body += ["## Not referenced (" + str(len(tail)) + ")", "",
                 "Harvested, but no model, notebook or pipeline in these workspaces "
                 "reads or writes them.", "",
                 ", ".join("`" + str(w.nodes.get(t, {}).get("name", t)) + "`"
                           for t in tail), ""]
    users = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "uses"]
    if users:
        body += ["## Used by", ""] + ["- " + w.link(u) for u in sorted(set(users))] + [""]
    w.write(node["kind"], w.slug[nid], _front(w, nid, node, {}), body)


def _table_page(w: Wiki, nid: str, node: dict) -> None:
    fed = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "feeds"]
    read = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "reads"]
    models = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "sources_from"]
    body = ["# " + node["name"], "",
            "Table `" + str(Wiki._attr(node, "schema", "dbo")) + "." + node["name"]
            + "` in " + str(Wiki._attr(node, "store", "")) + ".", ""]
    for title, rows in (("Written by", fed), ("Read by", read)):
        if rows:
            body += ["## " + title, ""] + ["- " + w.link(r) for r in sorted(set(rows))] + [""]
    if models:
        # a model_table links to its model page, so name both or the link reads wrong
        body += ["## Used by semantic models", ""]
        body += ["- " + w.link(m, (w.nodes.get(m, {}).get("workspace") and
                                   _owner_label(w, m)) or w.nodes.get(m, {}).get("name", m))
                 for m in sorted(set(models))]
        body.append("")
    if not (fed or read or models):
        body += ["Nothing in the harvested workspaces reads or writes this table.", ""]
    cols = Wiki._attr(node, "columns") or []
    if cols:
        stats = Wiki._attr(node, "stats") or {}
        values = Wiki._attr(node, "values") or {}
        ndv = Wiki._attr(node, "n_distinct") or {}
        n_rows = Wiki._attr(node, "n_rows")
        body += ["## Columns", "",
                 (("Rows: " + format(int(n_rows), ",") + ". ") if n_rows is not None else "")
                 + "Profiled from the Delta log"
                 + (" " + str(Wiki._attr(node, "profiled_at"))[:10] if Wiki._attr(node, "profiled_at") else "")
                 + ".", "",
                 "| column | type | distinct | min | max | values |",
                 "|--------|------|----------|-----|-----|--------|"]
        for c in cols[:80]:
            name = str(c.get("name"))
            st = stats.get(name) or {}
            vals = [str(v).replace("|", "/") for v in (values.get(name) or [])]
            body.append("| " + name + " | " + str(c.get("type", "")) + " | "
                        + str(ndv.get(name, "")) + " | " + str(st.get("min", ""))[:24]
                        + " | " + str(st.get("max", ""))[:24] + " | "
                        + ", ".join(vals[:8]) + (" ..." if len(vals) > 8 else "") + " |")
        body.append("")
    w.write("lakehouse_table", w.slug[nid], _front(w, nid, node, {
        "schema": Wiki._attr(node, "schema"), "store": Wiki._attr(node, "store")}), body)


def _owner_label(w: Wiki, nid: str) -> str:
    """'Sales Model / Sales' - a model table named together with the model it lives in."""
    node = w.nodes.get(nid, {})
    parent = w.nodes.get(node.get("parent_id") or "", {})
    if parent.get("name"):
        return parent["name"] + " / " + node.get("name", "")
    return node.get("name", nid)


def _notebook_page(w: Wiki, nid: str, node: dict) -> None:
    out = w.out_edges.get(nid, [])
    reads = [d for d, rel, _wt, _a in out if rel == "reads"]
    feeds = [d for d, rel, _wt, _a in out if rel == "feeds"]
    mentions = [d for d, rel, _wt, _a in out if rel == "mentions"]
    runners = [s for s, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "runs"]
    body = ["# " + node["name"], ""]
    if Wiki._attr(node, "default_lakehouse"):
        body += ["Default lakehouse: " + str(Wiki._attr(node, "default_lakehouse")) + ".", ""]
    for title, rows in (("Writes", feeds), ("Reads", reads), ("Run by", runners)):
        if rows:
            body += ["## " + title, ""] + ["- " + w.link(r) for r in sorted(set(rows))] + [""]
    if mentions:
        body += ["## Mentions", ""] + ["- " + w.link(m) for m in sorted(set(mentions))] + [""]
    body += ["> Table references in notebooks are recovered by pattern matching over the "
             "code. A name built from a variable or an f-string is not visible here.", ""]
    w.write("notebook", w.slug[nid], _front(w, nid, node, {}), body)


def _pipeline_page(w: Wiki, nid: str, node: dict) -> None:
    out = w.out_edges.get(nid, [])
    body = ["# " + node["name"], "",
            str(Wiki._attr(node, "n_activities", 0)) + " activities: "
            + ", ".join(Wiki._attr(node, "activity_types", []) or ["-"]) + ".", ""]
    for title, rels in (("Runs", ("runs",)), ("Refreshes", ("refreshes",)),
                        ("Writes", ("feeds",)), ("Reads", ("reads",))):
        rows = [d for d, rel, _wt, _a in out if rel in rels]
        if rows:
            body += ["## " + title, ""] + ["- " + w.link(r) for r in sorted(set(rows))] + [""]
    w.write("pipeline", w.slug[nid], _front(w, nid, node, {}), body)


def _simple_page(w: Wiki, nid: str, node: dict, kind: str) -> None:
    out = w.out_edges.get(nid, [])
    body = ["# " + node["name"], ""]
    uses = [d for d, rel, _wt, _a in out if rel in ("uses", "sources_from")]
    if uses:
        body += ["## Uses", ""] + ["- " + w.link(u) for u in sorted(set(uses))] + [""]
    used_by = [s for s, rel, _wt, _a in w.in_edges.get(nid, []) if rel in ("uses", "refreshes")]
    if used_by:
        body += ["## Used by", ""] + ["- " + w.link(u) for u in sorted(set(used_by))] + [""]
    w.write(kind, w.slug[nid], _front(w, nid, node, {}), body)


def _workspace_page(w: Wiki, nid: str, node: dict) -> None:
    by_kind: Dict[str, List[str]] = defaultdict(list)
    for dst, rel, _wt, _a in w.out_edges.get(nid, []):
        if rel == "contains" and dst in w.nodes:
            by_kind[w.nodes[dst]["kind"]].append(dst)
    body = ["# " + node["name"], ""]
    for kind in sorted(by_kind):
        rows = sorted(by_kind[kind], key=lambda x: w.nodes[x]["name"])
        used = [r for r in rows if not w.is_tail(r)]
        tail = [r for r in rows if w.is_tail(r)]
        body += ["## " + kind.replace("_", " ") + " (" + str(len(rows)) + ")", ""]
        body += ["- " + w.link(r) for r in used]
        if tail:
            # A measureless model something still points at, or a store whose tables
            # nobody reads: named,
            # so the workspace inventory is complete, but not worth a page.
            body += ["", "Empty or unreferenced, no page: "
                     + ", ".join("`" + w.nodes[r]["name"] + "`" for r in tail)]
        body.append("")
    w.write("workspace", w.slug[nid], _front(w, nid, node, {}), body)


def _front(w: Wiki, nid: str, node: dict, extra: dict) -> dict:
    front = {
        "id": nid, "kind": node["kind"], "name": node["name"],
        "tags": [node["kind"]] + ([str(node["endorsement"]).lower()]
                                  if node.get("endorsement") else []),
        "workspace": node.get("workspace"), "item_id": node.get("item_id"),
        "endorsement": node.get("endorsement"), "owner": node.get("owner"),
        "modified_at": node.get("modified_at"),
        "views_28d": w.views.get(node.get("item_id")),
        "url": w.portal_url(node),
    }
    front.update(extra)
    return front


def _index_page(w: Wiki, con) -> None:
    body = ["# Context layer", "",
            "Harvested from Fabric and ranked. Start at a term, then follow the links.", ""]
    body += ["## What is in here", "",
             "`with a page` is what something in these workspaces actually refers to; "
             "the rest was harvested and is listed on its parent's page.", "",
             "| kind | with a page | harvested |", "|------|-------------|-----------|"]
    for kind, shown, count in con.execute(
            "SELECT kind, count(*) FILTER (WHERE tier < 3), count(*) "
            "FROM nodes GROUP BY 1 ORDER BY 3 DESC").fetchall():
        body.append("| " + kind.replace("_", " ") + " | " + str(shown)
                    + " | " + str(count) + " |")
    body.append("")

    top = con.execute("SELECT term_id, label, n_definitions, views, conflicting FROM terms "
                      "ORDER BY views DESC, n_definitions DESC LIMIT 20").fetchall()
    if top:
        body += ["## Most used terms", "",
                 "| term | definitions | views (28d) | conflict |",
                 "|------|-------------|-------------|----------|"]
        body += ["| [[" + t[0] + "|" + w.term_name(t[0]) + "]] | " + str(t[2]) + " | "
                 + str(t[3]) + " | " + ("yes" if t[4] else "") + " |" for t in top]
        body.append("")

    conflicts = con.execute(
        "SELECT term_id, label, n_definitions, n_distinct_expr, n_items FROM terms "
        "WHERE conflicting ORDER BY n_distinct_expr DESC, n_definitions DESC").fetchall()
    body += ["## Terms whose definitions disagree (" + str(len(conflicts)) + ")", ""]
    if conflicts:
        body += ["| term | definitions | distinct expressions | items |",
                 "|------|-------------|----------------------|-------|"]
        body += ["| [[" + c[0] + "|" + w.term_name(c[0]) + "]] | " + str(c[2]) + " | "
                 + str(c[3]) + " | " + str(c[4]) + " |" for c in conflicts]
    else:
        body += ["None found.", ""]
    body.append("")

    workspaces = [nid for nid, n in w.nodes.items() if n["kind"] == "workspace"]
    if workspaces:
        body += ["## Workspaces", ""] + ["- " + w.link(x) for x in sorted(workspaces)] + [""]
    write_text(os.path.join(w.out, "index.md"), "\n".join(body) + "\n")


def _claude_page(w: Wiki, con) -> None:
    n_terms = con.execute("SELECT count(*) FROM terms").fetchone()[0]
    n_conf = con.execute("SELECT count(*) FROM terms WHERE conflicting").fetchone()[0]
    n_tail = con.execute("SELECT count(*) FROM nodes WHERE tier >= 3").fetchone()[0]
    text = """# How to answer questions from this folder

This is a **context layer**: it was harvested automatically from Microsoft Fabric, not
written by hand. Nobody authored these pages, and nothing here was reviewed.

There are {terms} business terms, {conf} of which have definitions that disagree.

**Not everything harvested has a page here.** {tail} nodes - lakehouse tables no model,
notebook or pipeline touches, SQL endpoints - are listed on their store or workspace page
and nowhere else. (The semantic models Fabric auto-creates beside a lakehouse are not here
at all: they define no measure, so they were dropped, not demoted.) They are still
in the database, and named under their store in `context.md`.
The absence of a page means nothing in these workspaces refers to it, not that it is gone.

## Two ways in

- **Reading**: start at `terms/<term>.md`. It ranks every definition of that term and shows
  the DAX. Follow the wikilinks: a measure links to its model page, a model to the tables
  it reads, a table to the notebook that writes it. `index.md` lists what was harvested
  and every term with a conflict.
- **Asking**: the same facts, from the database these pages were rendered from, through
  `context.md`, the same context as one file: `python -m ask context` fetches it, and
  `python -m ask dax <workspace_id>/<item_id> "<EVALUATE ...>"` runs the ranked
  definition against its model. Those two are the whole client. In Claude Code the
  `fabric-context` skill drives them.

## What the ranking means

Definitions are ranked by four signals, the way a search engine ranks pages:

- **authority** - certified beats promoted beats nothing; documented beats undocumented;
  a measure in a semantic model beats one defined inside a single report.
- **popularity** - how often the measure was actually evaluated over 28 days, where the
  workspace's monitoring log covers it, plus how often the reports that use it were opened
  and the model that defines it was opened or refreshed. A workspace without monitoring
  contributes only the second half, so a measure nobody watches is not penalised.
- **relevance** - how many reports and visuals reference it, plus an exact name match.
- **freshness** - how recently the owning item changed.

**Rank is not correctness.** A popular, certified, wrong definition still ranks first. When
a term is flagged as conflicting, say so and show the competing expressions rather than
picking one.

## What is unreliable

- Table references inside notebooks are found by pattern matching over code. A name built
  from a variable or an f-string is missing.
- Usage covers 28 days of audit events only.
- Anything shown as `unresolved` is a reference that could not be bound to a harvested item.
  A store marked `external` is bound to by something harvested but was not harvested itself.
- Terms merge on their words, with word order and common synonyms ignored. Two spellings
  of one idea that share no word stay apart; `src/aliases.yaml` can pin them together.

## Questions this folder answers

- *Where is X defined and which definition should I trust?* - `terms/<x>.md`, or
  the `### term:` section of `context.md`: quote the top expression, then say whether
  the others disagree.
- *Which reports use X?* - the "Used in reports" table on the term page, or `usage`.
- *What feeds X?* - the "Upstream" list on the term page, or `lineage`.
- *What is X for <filter>?* - `model` for the schema and filter values, then `dax` calling
  the ranked measure by name. Only the query side does this; the pages hold no numbers.
""".format(terms=n_terms, conf=n_conf, tail=n_tail)
    write_text(os.path.join(w.out, "CLAUDE.md"), text)


# ---------------------------------------------------------------- context.md

MAX_COLUMNS = 80        # per table, as the table pages cap them
MAX_VALUES = 8          # distinct values quoted per column
MAX_FIELDS = 40         # fields listed per report


def _cell(value, limit: int = 200) -> str:
    """One markdown table cell. A pipe or a newline inside a harvested name would end the
    row early, so both are neutralised here - and DAX never goes in a cell at all."""
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|").strip()
    return (text[:limit] + "...") if len(text) > limit else text


def _fence(expression) -> List[str]:
    text = str(expression or "").strip()
    return ["```dax", text or "(no expression captured)", "```"]


def _dash(value) -> str:
    """A missing field reads as `-`, not as the word None."""
    return str(value) if value not in (None, "") else "-"


def _eattr(attrs, key: str, default=None):
    """One key out of an edge's attrs, which arrive as JSON text or as a dict."""
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            return default
    return attrs.get(key, default) if isinstance(attrs, dict) else default


def _ref(w: Wiki, nid: str) -> str:
    """A node named plainly. The wiki links; one file has nowhere to link to, so a thing is
    named the way someone would type it into `ask`."""
    if nid.startswith("unresolved:"):
        return "`" + nid.split("/", 1)[-1] + "` (unresolved)"
    node = w.nodes.get(nid)
    if not node:
        return "`" + nid + "`"
    kind = node["kind"]
    if kind == "lakehouse_table":
        return (str(Wiki._attr(node, "store", "")) + "."
                + str(Wiki._attr(node, "schema", "dbo")) + "." + node["name"])
    if kind in ("measure", "report_measure"):
        return "[" + node["name"] + "]"
    if kind in ("model_table", "column"):
        return _owner_label(w, nid)
    return node["name"] + " (" + kind.replace("_", " ") + ")"


def _refs(w: Wiki, nids) -> str:
    return ", ".join(_ref(w, n) for n in sorted(set(nids), key=lambda n: _ref(w, n)))


def _context_page(w: Wiki, con, path: str) -> int:
    """The whole context as one markdown file. Returns its size in bytes.

    Same facts as the wiki, addressed to a different reader. An agent that can read this
    file needs the query side for one thing only: `ask dax`, for a number. There is no
    other route - a table no model covers is described here and computed nowhere. The
    long tail is named but not detailed; see `Wiki.is_tail`.
    """
    lines: List[str] = []
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    workspaces = []
    try:
        workspaces = json.loads(meta.get("workspaces") or "[]")
    except ValueError:
        pass
    names = [str(ws.get("name")) for ws in workspaces] or ["Fabric"]

    lines += ["# Fabric context: " + ", ".join(names[:6])
              + (" and " + str(len(names) - 6) + " more" if len(names) > 6 else ""), ""]
    lines += _context_header(w, con, meta, workspaces)
    lines += _context_guide(con)
    lines += _context_terms(w, con)
    lines += _context_models(w, con)
    lines += _context_reports(w)
    lines += _context_flows(w)
    lines += _context_stores(w)

    text = "\n".join(lines).rstrip() + "\n"
    write_text(path, text)
    return len(text.encode("utf-8"))


def _context_header(w: Wiki, con, meta: Dict[str, str], workspaces: List[dict]) -> List[str]:
    n_terms, n_conf = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE conflicting) FROM terms").fetchone()
    kinds = dict(con.execute(
        "SELECT kind, count(*) FILTER (WHERE tier < 3) FROM nodes GROUP BY 1").fetchall())
    tail = con.execute("SELECT count(*) FROM nodes WHERE tier >= 3").fetchone()[0]
    lines = ["built_at: " + str(meta.get("built_at", "?"))
             + " | schema_version: " + str(meta.get("schema_version", "?"))
             + " | activity: " + str(meta.get("activity_window_days", "?")) + " days"
             + (" (" + str(meta.get("activity_from"))[:10] + " .. "
                + str(meta.get("activity_to"))[:10] + ")"
                if meta.get("activity_from") else " (no audit log)"), ""]
    for ws in workspaces:
        lines.append("- workspace " + str(ws.get("name")) + " (" + str(ws.get("id")) + ")"
                     + (", harvested " + str(ws.get("harvested_at"))[:19]
                        if ws.get("harvested_at") else ""))
    lines += ["",
              "terms: " + str(n_terms) + " (" + str(n_conf) + " conflicting)"
              + " | models: " + str(kinds.get("semantic_model", 0))
              + " | reports: " + str(kinds.get("report", 0))
              + " | notebooks: " + str(kinds.get("notebook", 0))
              + " | pipelines: " + str(kinds.get("pipeline", 0))
              + " | stores: " + str(kinds.get("lakehouse", 0) + kinds.get("warehouse", 0))
              + " | tables: " + str(kinds.get("lakehouse_table", 0))
              + " | tier-3 nodes named but not detailed: " + str(tail), ""]
    return lines


def _context_guide(con) -> List[str]:
    """How to answer from this file. The wiki says the same thing to a person reading pages
    (`_claude_page`); this says it to an agent holding one file and a terminal."""
    n_conf = con.execute("SELECT count(*) FROM terms WHERE conflicting").fetchone()[0]
    return ("""## How to use this file

This is a **context layer**, harvested automatically from Microsoft Fabric. Nobody wrote
these sections by hand and nothing in them was reviewed.

**Find things by heading.** `### term: <term_id>` ranks every definition of a business term
and shows its DAX. `### model: <name>` carries the model's `item_id` and `workspace_id` -
what a DAX query executes against - its tables, its filter values and its measures.
`### store: <name>` and `#### table:` carry the physical tables and who writes them.

**Rank 1 is the answer.** Definitions are ranked by four signals, the way a search engine
ranks pages: **authority** (certified beats promoted beats nothing; a model measure beats
one defined inside a single report), **popularity** (how often the measure was actually
evaluated, plus report opens and model refreshes), **relevance** (reports and visuals
referencing it, plus an exact name match) and **freshness**. Take rank 1 and say so.

**Rank is not correctness.** A popular, certified, wrong definition still ranks first.
{conf} terms here have definitions that disagree; when one of them is the answer, disclose
it in one line - which definition you used and that the others differ - rather than handing
back a menu.

**This file holds no numbers.** To compute one, copy the `run:` line from the model's
section - it already carries the two ids - and call the ranked measure by name:

    python -m ask dax <workspace_id>/<item_id> "EVALUATE ROW(\\"v\\", [<measure>])"

That is the only other thing to run. Filter literals come from the `values:` on a column;
when a column has none, one more DAX query fetches them:
`EVALUATE TOPN(50, VALUES('<table>'[<column>]))`. Never invent a literal.

**When no measure covers it, say so.** There is no SQL here and no second route to a
number. A term with no definition, or a table no semantic model reads, has nothing in this
tenant that agrees what its number means - so the answer is that, plus what this file does
know: the columns, who writes the table, what it feeds. Computing it anyway would invent
the definition this layer exists to find.

**Tier.** **1** feeds a semantic model, **2** is read or written by a notebook or pipeline,
**3** is harvested and nothing in these workspaces refers to it. Tier-3 nodes are named
under their store or model and detailed nowhere - the absence means nothing refers to them,
not that they are gone. A tier-3 table is a describe-only answer by definition.

**What is unreliable.** Table references inside notebooks are recovered by pattern matching
over code, so a name built from a variable is missing. Usage covers the audit window above
only. Anything marked `unresolved` is a reference that could not be bound to a harvested
item, and a store marked `external` was bound to but never harvested. Terms merge on their
words, so two spellings of one idea that share no word stay apart.
""".format(conf=n_conf)).split("\n")


def _context_terms(w: Wiki, con) -> List[str]:
    terms = con.execute(
        "SELECT term_id, label, n_definitions, n_distinct_expr, n_items, conflicting, "
        "       views, n_reports FROM terms ORDER BY views DESC, term_id").fetchall()
    if not terms:
        return []
    defs: Dict[str, List[tuple]] = defaultdict(list)
    for row in con.execute(
            "SELECT term_id, rank, def_id, name, kind, owner_item_id, owner_item_name, "
            "       workspace, table_name, endorsement, views, n_reports, queries, score, "
            "       expression, description FROM definitions ORDER BY term_id, rank"
    ).fetchall():
        defs[row[0]].append(row)
    aliases: Dict[str, List[str]] = defaultdict(list)
    for term_id, alias in con.execute(
            "SELECT DISTINCT term_id, alias FROM aliases ORDER BY 1, 2").fetchall():
        aliases[term_id].append(alias)
    reports: Dict[str, List[tuple]] = defaultdict(list)
    for term_id, name, workspace, views in con.execute(
            "SELECT d.term_id, r.name, r.workspace, max(coalesce(iv.views, 0)) "
            "  FROM definitions d "
            "  JOIN measure_usage mu ON mu.def_id = d.def_id "
            "  JOIN nodes r ON r.id = 'report:' || mu.report_id "
            "  LEFT JOIN item_views iv ON iv.item_id = mu.report_id "
            " GROUP BY 1, 2, 3 ORDER BY 4 DESC").fetchall():
        reports[term_id].append((name, workspace, views))

    lines = ["## Terms (" + str(len(terms)) + ")", "",
             "| term | label | definitions | conflicting | rank 1 | in | views 28d |",
             "|------|-------|-------------|-------------|--------|----|-----------|"]
    for term_id, label, n_defs, _nd, _ni, conflicting, views, _nr in terms:
        top = (defs.get(term_id) or [(None,) * 16])[0]
        lines.append("| " + _cell(term_id) + " | " + _cell(label or term_label(term_id))
                     + " | " + str(n_defs) + " | " + ("yes" if conflicting else "")
                     + " | " + _cell(top[3]) + " | " + _cell(top[6])
                     + " | " + str(views) + " |")
    lines.append("")

    for term_id, label, n_defs, n_distinct, n_items, conflicting, views, n_reports in terms:
        rows = defs.get(term_id) or []
        if not rows:
            continue
        lines += ["### term: " + term_id, "",
                  "label: " + str(label or term_label(term_id))
                  + " | definitions: " + str(n_defs) + " across " + str(n_items) + " items"
                  + " | conflicting: " + ("yes, " + str(n_distinct) + " distinct expressions"
                                          if conflicting else "no, all agree")
                  + " | views 28d: " + str(views) + " | reports: " + str(n_reports), ""]
        if len(aliases.get(term_id, [])) > 1:
            lines += ["also known as: " + ", ".join(aliases[term_id]), ""]
        lines += ["| rank | measure | model | workspace | endorsement | score | views | "
                  "reports | queries |",
                  "|------|---------|-------|-----------|-------------|-------|-------|"
                  "---------|---------|"]
        for r in rows:
            lines.append("| " + str(r[1]) + " | " + _cell(r[3]) + " | " + _cell(r[6])
                         + " | " + _cell(r[7]) + " | " + (_cell(r[9]) if r[9] else "-")
                         + " | " + str(round(r[13] or 0, 2)) + " | " + str(r[10] or 0)
                         + " | " + str(r[11] or 0) + " | " + str(r[12] or 0) + " |")
        lines.append("")
        for r in rows:
            lines += ["#### " + str(r[1]) + ". " + str(r[3]) + " - " + str(r[6])
                      + " (" + str(r[5]) + ")"
                      + (", table " + str(r[8]) if r[8] else "")
                      + (" - defined inside a report" if r[4] == "report_measure" else ""), ""]
            if r[15]:
                lines += [str(r[15]).replace("\n", " "), ""]
            lines += _fence(r[14]) + [""]
        if reports.get(term_id):
            lines += ["used in reports: "
                      + ", ".join(str(n) + " (" + str(v) + " views)"
                                  for n, _ws, v in reports[term_id][:20]), ""]
        upstream = _upstream_of(w, con, [r[2] for r in rows])
        if upstream:
            lines += ["upstream: " + ", ".join(_ref(w, nid) for nid, _kind in upstream), ""]
    return lines


def _context_models(w: Wiki, con) -> List[str]:
    models = sorted([(nid, n) for nid, n in w.nodes.items()
                     if n["kind"] == "semantic_model" and not w.is_tail(nid)],
                    key=lambda kv: (kv[1]["workspace"] or "", kv[1]["name"]))
    empty = sorted(n["name"] for nid, n in w.nodes.items()
                   if n["kind"] == "semantic_model" and w.is_tail(nid))
    if not models and not empty:
        return []
    measures: Dict[str, List[tuple]] = defaultdict(list)
    for row in con.execute(
            "SELECT owner_item_id, name, table_name, term_id, rank, conflicting, "
            "       expression, description FROM definitions WHERE kind = 'measure' "
            " ORDER BY table_name, name").fetchall():
        measures[row[0]].append(row)

    lines = ["## Semantic models (" + str(len(models)) + ")", ""]
    for nid, node in models:
        lines += ["### model: " + node["name"], "",
                  "item_id: " + _dash(node.get("item_id"))
                  + " | workspace: " + _dash(node.get("workspace"))
                  + " | workspace_id: " + _dash(w._workspace_id(node))
                  + " | storage: " + _dash(Wiki._attr(node, "storage_mode"))
                  + " | endorsement: " + _dash(node.get("endorsement"))
                  + (" | owner: " + str(node["owner"]) if node.get("owner") else "")
                  + (" | modified: " + str(node["modified_at"])[:10]
                     if node.get("modified_at") else "")
                  + " | views 28d: " + str(w.views.get(node.get("item_id"), 0)), ""]
        if node.get("description"):
            lines += [str(node["description"]).replace("\n", " "), ""]
        # The two ids in the order `ask dax` takes them, so a number is one copy away.
        lines += ["run: python -m ask dax " + _dash(w._workspace_id(node)) + "/"
                  + _dash(node.get("item_id"))
                  + ' "EVALUATE ROW(\\"v\\", [<measure>])"', ""]
        lines += _context_model_tables(w, nid)
        for row in measures.get(node.get("item_id"), []):
            lines += ["#### measure: [" + str(row[1]) + "] on " + str(row[2]), "",
                      "defines term `" + str(row[3]) + "`, rank " + str(row[4])
                      + (", other definitions disagree" if row[5] else ""), ""]
            if row[7]:
                lines += [str(row[7]).replace("\n", " "), ""]
            lines += _fence(row[6]) + [""]
        used_by = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel == "uses"]
        if used_by:
            lines += ["used by: " + _refs(w, used_by), ""]
    if empty:
        lines += ["Empty or unreferenced models, not detailed (" + str(len(empty)) + "): "
                  + ", ".join("`" + n + "`" for n in empty[:60]), ""]
    return lines


def _context_model_tables(w: Wiki, nid: str) -> List[str]:
    tables = [(dst, w.nodes[dst]) for dst, rel, _wt, _a in w.out_edges.get(nid, [])
              if rel == "contains" and dst in w.nodes and w.nodes[dst]["kind"] == "model_table"]
    lines: List[str] = []
    rels: List[str] = []
    for tid, table in sorted(tables, key=lambda t: t[1]["name"]):
        sources = [dst for dst, rel, _wt, _a in w.out_edges.get(tid, [])
                   if rel == "sources_from"]
        n_rows = Wiki._attr(table, "n_rows")
        lines += ["#### table: " + table["name"]
                  + " (" + str(Wiki._attr(table, "mode", "?"))
                  + (", " + format(int(n_rows), ",") + " rows" if n_rows is not None else "")
                  + ")" + (" <- " + _refs(w, sources) if sources else ""), ""]
        if table.get("description"):
            lines += [str(table["description"]).replace("\n", " "), ""]
        for cid, _rel, _wt, _a in [(d, r, x, a) for d, r, x, a in w.out_edges.get(tid, [])
                                   if r == "contains"][:MAX_COLUMNS]:
            col = w.nodes.get(cid)
            if not col or col["kind"] != "column":
                continue
            profile = Wiki._attr(col, "profile") or {}
            values = [str(v) for v in (profile.get("values") or [])][:MAX_VALUES]
            detail = ""
            if values:
                detail = " | values: " + ", ".join(values) + (
                    " ..." if len(profile.get("values") or []) > MAX_VALUES else "")
            elif profile.get("min") is not None or profile.get("max") is not None:
                detail = (" | range " + str(profile.get("min"))[:24] + " .. "
                          + str(profile.get("max"))[:24])
            if profile.get("n_distinct") is not None:
                detail += " | distinct: " + str(profile["n_distinct"])
            lines.append("- " + col["name"] + ": "
                         + str(Wiki._attr(col, "data_type", "?")) + detail)
        lines.append("")
        for dst, rel, _wt, attrs in w.out_edges.get(tid, []):
            if rel == "relates_to" and dst in w.nodes:
                rels.append(table["name"] + "[" + str(_eattr(attrs, "from_column", "?"))
                            + "] -> " + w.nodes[dst]["name"] + "["
                            + str(_eattr(attrs, "to_column", "?")) + "]")
    if rels:
        lines += ["relationships: " + "; ".join(sorted(rels)), ""]
    return lines


def _context_reports(w: Wiki) -> List[str]:
    items = sorted([(nid, n) for nid, n in w.nodes.items()
                    if n["kind"] in ("report", "dashboard") and not w.is_tail(nid)],
                   key=lambda kv: (kv[1]["kind"], kv[1]["workspace"] or "", kv[1]["name"]))
    if not items:
        return []
    lines = ["## Reports and dashboards (" + str(len(items)) + ")", ""]
    for nid, node in items:
        out = w.out_edges.get(nid, [])
        lines += ["### " + node["kind"] + ": " + node["name"], "",
                  "item_id: " + _dash(node.get("item_id"))
                  + " | workspace: " + _dash(node.get("workspace"))
                  + " | views 28d: " + str(w.views.get(node.get("item_id"), 0)), ""]
        model = [dst for dst, rel, _wt, _a in out if rel == "uses"]
        if model:
            lines += ["reads: " + _refs(w, model), ""]
        pages = [(dst, w.nodes[dst]) for dst, rel, _wt, _a in out
                 if rel == "contains" and dst in w.nodes and w.nodes[dst]["kind"] == "page"]
        if pages:
            lines += ["pages: " + ", ".join(
                p["name"] + " (" + str(Wiki._attr(p, "n_visuals", 0)) + " visuals)"
                for _pid, p in sorted(pages, key=lambda x: x[1]["name"])), ""]
        fields: Dict[str, int] = {}
        for pid, _p in pages:
            for vid, rel, _wt, _a in w.out_edges.get(pid, []):
                if rel != "contains":
                    continue
                for dst, vrel, weight, _at in w.out_edges.get(vid, []):
                    if vrel == "references":
                        fields[dst] = fields.get(dst, 0) + int(weight or 1)
        if fields:
            top = sorted(fields.items(), key=lambda kv: -kv[1])[:MAX_FIELDS]
            lines += ["fields used: " + ", ".join(_ref(w, f) + " (" + str(c) + ")"
                                                  for f, c in top), ""]
        for mid in [dst for dst, rel, _wt, _a in out
                    if rel == "contains" and dst in w.nodes
                    and w.nodes[dst]["kind"] == "report_measure"]:
            lines += ["#### report measure: [" + w.nodes[mid]["name"] + "]", ""]
            lines += _fence(Wiki._attr(w.nodes[mid], "expression", "")) + [""]
    return lines


def _context_flows(w: Wiki) -> List[str]:
    kinds = ("notebook", "pipeline", "dataflow")
    items = sorted([(nid, n) for nid, n in w.nodes.items()
                    if n["kind"] in kinds and not w.is_tail(nid)],
                   key=lambda kv: (kv[1]["kind"], kv[1]["workspace"] or "", kv[1]["name"]))
    if not items:
        return []
    lines = ["## Notebooks, pipelines and dataflows (" + str(len(items)) + ")", ""]
    for nid, node in items:
        out = w.out_edges.get(nid, [])
        lines += ["### " + node["kind"] + ": " + node["name"], "",
                  "item_id: " + _dash(node.get("item_id"))
                  + " | workspace: " + _dash(node.get("workspace"))
                  + (" | default lakehouse: " + str(Wiki._attr(node, "default_lakehouse"))
                     if Wiki._attr(node, "default_lakehouse") else "")
                  + (" | " + str(Wiki._attr(node, "n_activities", 0)) + " activities: "
                     + ", ".join(Wiki._attr(node, "activity_types", []) or ["-"])
                     if node["kind"] == "pipeline" else ""), ""]
        for title, rels in (("writes", ("feeds",)), ("reads", ("reads", "sources_from")),
                            ("runs", ("runs",)), ("refreshes", ("refreshes",)),
                            ("uses", ("uses",)), ("mentions", ("mentions",))):
            rows = [d for d, rel, _wt, _a in out if rel in rels]
            if rows:
                lines.append("- " + title + ": " + _refs(w, rows))
        runners = [s for s, rel, _wt, _a in w.in_edges.get(nid, [])
                   if rel in ("runs", "uses")]
        if runners:
            lines.append("- run by: " + _refs(w, runners))
        lines.append("")
        if node["kind"] == "notebook":
            lines += ["> Table references in notebooks are recovered by pattern matching "
                      "over the code; a name built from a variable is not visible here.", ""]
    return lines


def _context_stores(w: Wiki) -> List[str]:
    stores = sorted([(nid, n) for nid, n in w.nodes.items()
                     if n["kind"] in ("lakehouse", "warehouse")],
                    key=lambda kv: (kv[1]["workspace"] or "", kv[1]["name"]))
    if not stores:
        return []
    lines = ["## Stores (" + str(len(stores)) + ")", ""]
    for nid, node in stores:
        tables = [dst for dst, rel, _wt, _a in w.out_edges.get(nid, []) if rel == "contains"]
        used = sorted([t for t in tables if not w.is_tail(t)],
                      key=lambda t: w.nodes.get(t, {}).get("name", ""))
        tail = sorted([t for t in tables if w.is_tail(t)],
                      key=lambda t: w.nodes.get(t, {}).get("name", ""))
        lines += ["### store: " + node["name"] + " (" + node["kind"] + ")", "",
                  "item_id: " + _dash(node.get("item_id"))
                  + " | workspace: " + _dash(node.get("workspace"))
                  + " | workspace_id: " + _dash(w._workspace_id(node))
                  + " | tables: " + str(len(tables)) + " (" + str(len(used))
                  + " referenced)", ""]
        if Wiki._attr(node, "external"):
            lines += ["This store was not harvested: something in a harvested workspace "
                      "binds to it (" + str(Wiki._attr(node, "via", "reference"))
                      + "), so only the tables bound to are known.", ""]
        for tid in used:
            lines += _context_store_table(w, tid)
        if tail:
            lines += ["not referenced (" + str(len(tail)) + "), harvested but no model, "
                      "notebook or pipeline in these workspaces reads or writes them: "
                      + ", ".join("`" + str(w.nodes.get(t, {}).get("name", t)) + "`"
                                  for t in tail), ""]
    return lines


def _context_store_table(w: Wiki, nid: str) -> List[str]:
    node = w.nodes.get(nid)
    if not node:
        return []
    n_rows = Wiki._attr(node, "n_rows")
    lines = ["#### table: " + _ref(w, nid)
             + " (tier " + str(node.get("tier") or 1)
             + (", " + format(int(n_rows), ",") + " rows" if n_rows is not None else "")
             + (", profiled " + str(Wiki._attr(node, "profiled_at"))[:10]
                if Wiki._attr(node, "profiled_at") else "") + ")", ""]
    for title, rels in (("written by", ("feeds",)), ("read by", ("reads",)),
                        ("used by models", ("sources_from",))):
        rows = [src for src, rel, _wt, _a in w.in_edges.get(nid, []) if rel in rels]
        if rows:
            lines.append("- " + title + ": " + _refs(w, rows))
    lines.append("")
    cols = Wiki._attr(node, "columns") or []
    if not cols:
        return lines
    stats = Wiki._attr(node, "stats") or {}
    values = Wiki._attr(node, "values") or {}
    ndv = Wiki._attr(node, "n_distinct") or {}
    lines += ["| column | type | distinct | min | max | values |",
              "|--------|------|----------|-----|-----|--------|"]
    for c in cols[:MAX_COLUMNS]:
        name = str(c.get("name"))
        st = stats.get(name) or {}
        vals = [_cell(v, 40) for v in (values.get(name) or [])]
        lines.append("| " + _cell(name) + " | " + _cell(c.get("type", "")) + " | "
                     + str(ndv.get(name, "")) + " | " + _cell(st.get("min", ""), 24)
                     + " | " + _cell(st.get("max", ""), 24) + " | "
                     + ", ".join(vals[:MAX_VALUES])
                     + (" ..." if len(vals) > MAX_VALUES else "") + " |")
    lines.append("")
    return lines


def check_links(out_dir: str) -> List[Tuple[str, str]]:
    """[(page, target)] for every wikilink with no page behind it."""
    import re
    pages = set()
    for base, _dirs, files in os.walk(out_dir):
        for name in files:
            if name.endswith(".md"):
                pages.add(name[:-3])
    broken: List[Tuple[str, str]] = []
    pattern = re.compile(r"\[\[([^\]|#]+)")
    for base, _dirs, files in os.walk(out_dir):
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as fh:
                for match in pattern.finditer(fh.read()):
                    if match.group(1) not in pages:
                        broken.append((name, match.group(1)))
    return broken
