"""Report definitions -> pages, visuals and the fields each visual puts on screen.

Two formats exist and they share nothing. PBIR is a folder of small JSON files, one per
page and per visual. PBIR-Legacy is a single report.json whose interesting parts are JSON
documents stored as strings inside it. Both end at the same place: a list of
(entity, property) field references per visual, resolved against the bound model.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterator, List, Optional, Tuple

from common import (Emitter, is_exact_term, node_id, norm_dax, read_json, read_text,
                    term_id, unresolved_id)
from parse_model import ModelIndex, extract_dax_refs

# The wrappers a field reference can appear under in either format.
_FIELD_KINDS = ("Measure", "Column", "HierarchyLevel", "Aggregation", "SparklineData")
_QUERY_REF = re.compile(r"^([^.]+)\.(.+)$")


def _iter_dicts(obj) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_dicts(value)


def _alias_map(obj) -> Dict[str, str]:
    """Legacy prototypeQuery aliases: From: [{Name: 's', Entity: 'Sales'}]."""
    aliases: Dict[str, str] = {}
    for node in _iter_dicts(obj):
        entries = node.get("From")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("Name") and entry.get("Entity"):
                    aliases[entry["Name"]] = entry["Entity"]
    return aliases


def field_refs(obj) -> List[Tuple[str, str, str]]:
    """[(entity, property, kind)] for every field reference anywhere in a visual document."""
    aliases = _alias_map(obj)
    out: List[Tuple[str, str, str]] = []
    for node in _iter_dicts(obj):
        for kind in _FIELD_KINDS:
            spec = node.get(kind)
            if not isinstance(spec, dict) or not spec.get("Property"):
                continue
            source = ((spec.get("Expression") or {}).get("SourceRef") or {})
            entity = source.get("Entity") or aliases.get(source.get("Source") or "", "")
            if not entity:
                inner = ((spec.get("Expression") or {}).get("Column")
                         or (spec.get("Expression") or {}).get("Measure") or {})
                source = (inner.get("Expression") or {}).get("SourceRef") or {}
                entity = source.get("Entity") or aliases.get(source.get("Source") or "", "")
            out.append((entity, spec["Property"], kind))
        ref = node.get("queryRef")
        if isinstance(ref, str) and "." in ref and not node.get("Property"):
            match = _QUERY_REF.match(ref)
            if match:
                out.append((match.group(1), match.group(2), "queryRef"))
    return out


def _bind_refs(refs, index: Optional[ModelIndex], vid: str, g: Emitter) -> int:
    """Emit visual -> measure/column edges. Unbound references become unresolved nodes so
    the miss rate stays visible."""
    counts: Dict[str, int] = {}
    for entity, prop, _kind in refs:
        target = None
        if index:
            target = index.measure(prop) or index.column(entity, prop)
        if not target:
            target = unresolved_id("field", (entity or "?") + "[" + prop + "]")
        counts[target] = counts.get(target, 0) + 1
    for target, count in counts.items():
        g.edge(vid, target, "references", weight=count, via="visual")
    return len(counts)


def parse_report(folder: str, item: Dict, ws_name: str, g: Emitter,
                 index_by_model: Dict[str, ModelIndex],
                 scanner_report: Optional[Dict] = None,
                 modified_at: Optional[str] = None) -> None:
    """Walk one report definition folder in either format."""
    scanner_report = scanner_report or {}
    guid = item["id"]
    name = item.get("displayName") or guid
    rid = node_id("report", guid)
    endorse = (scanner_report.get("endorsementDetails") or {}).get("endorsement")

    model_guid = _bound_model(folder, scanner_report)
    index = index_by_model.get(model_guid) if model_guid else None

    g.node(rid, "report", name, workspace=ws_name, item_id=guid,
           description=item.get("description") or scanner_report.get("description"),
           endorsement=endorse,
           owner=scanner_report.get("modifiedBy") or scanner_report.get("createdBy"),
           modified_at=modified_at or scanner_report.get("modifiedDateTime"),
           report_type=scanner_report.get("reportType"), format=item.get("format"))
    if model_guid:
        g.edge(rid, node_id("semantic_model", model_guid), "uses")

    pbir_pages = os.path.join(folder, "definition", "pages")
    if os.path.isdir(pbir_pages):
        _parse_pbir(folder, pbir_pages, rid, guid, ws_name, index, g)
    else:
        legacy = os.path.join(folder, "report.json")
        if os.path.exists(legacy):
            _parse_legacy(legacy, rid, guid, ws_name, index, g)

    _report_measures(folder, rid, guid, ws_name, index, g)


def _bound_model(folder: str, scanner_report: Dict) -> Optional[str]:
    """The semantic model GUID a report reads. definition.pbir names it directly; otherwise
    the scanner's report.datasetId is the fallback."""
    pbir = read_json(os.path.join(folder, "definition.pbir"), {}) or {}
    ref = (pbir.get("datasetReference") or {})
    by_conn = (ref.get("byConnection") or {})
    for key in ("pbiModelDatabaseName", "pbiModelVirtualServerName"):
        value = by_conn.get(key)
        if value and re.fullmatch(r"[0-9a-fA-F-]{36}", str(value)):
            return str(value)
    return scanner_report.get("datasetId")


def _parse_pbir(folder: str, pages_dir: str, rid: str, guid: str, ws_name: str,
                index: Optional[ModelIndex], g: Emitter) -> None:
    order = (read_json(os.path.join(pages_dir, "pages.json"), {}) or {}).get("pageOrder") or []
    for entry in sorted(os.listdir(pages_dir)):
        page_dir = os.path.join(pages_dir, entry)
        page_file = os.path.join(page_dir, "page.json")
        if not os.path.exists(page_file):
            continue
        page = read_json(page_file, {}) or {}
        pname = page.get("displayName") or entry
        pid = node_id("page", guid, entry)
        visuals_dir = os.path.join(page_dir, "visuals")
        visuals = sorted(os.listdir(visuals_dir)) if os.path.isdir(visuals_dir) else []
        g.node(pid, "page", pname, workspace=ws_name, item_id=guid, parent_id=rid,
               n_visuals=len(visuals), visibility=page.get("visibility"),
               position=order.index(entry) if entry in order else None)
        g.edge(rid, pid, "contains")
        for vname in visuals:
            vfile = os.path.join(visuals_dir, vname, "visual.json")
            if not os.path.exists(vfile):
                continue
            visual = read_json(vfile, {}) or {}
            spec = visual.get("visual") or {}
            vid = node_id("visual", guid, entry, vname)
            g.node(vid, "visual", _visual_title(visual) or spec.get("visualType") or vname,
                   workspace=ws_name, item_id=guid, parent_id=pid,
                   visual_type=spec.get("visualType"), page=pname)
            g.edge(pid, vid, "contains")
            _bind_refs(field_refs(visual), index, vid, g)


def _visual_title(visual: Dict) -> Optional[str]:
    try:
        title = ((visual.get("visual") or {}).get("visualContainerObjects") or {}).get("title")
        text = title[0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return str(text).strip("'")
    except Exception:                                  # noqa: BLE001 - titles are optional
        return None


def _parse_legacy(path: str, rid: str, guid: str, ws_name: str,
                  index: Optional[ModelIndex], g: Emitter) -> None:
    report = read_json(path, {}) or {}
    for section in report.get("sections") or []:
        entry = section.get("name") or ""
        pname = section.get("displayName") or entry
        pid = node_id("page", guid, entry)
        containers = section.get("visualContainers") or []
        g.node(pid, "page", pname, workspace=ws_name, item_id=guid, parent_id=rid,
               n_visuals=len(containers), position=section.get("ordinal"))
        g.edge(rid, pid, "contains")
        for i, container in enumerate(containers):
            config = _maybe_json(container.get("config"))
            single = (config.get("singleVisual") or {}) if isinstance(config, dict) else {}
            vname = (config.get("name") if isinstance(config, dict) else None) or ("v" + str(i))
            vid = node_id("visual", guid, entry, vname)
            g.node(vid, "visual", single.get("visualType") or vname, workspace=ws_name,
                   item_id=guid, parent_id=pid, visual_type=single.get("visualType"),
                   page=pname)
            g.edge(pid, vid, "contains")
            payload = [config,
                       _maybe_json(container.get("query")),
                       _maybe_json(container.get("dataTransforms")),
                       _maybe_json(container.get("filters"))]
            _bind_refs(field_refs(payload), index, vid, g)


def _maybe_json(value):
    """Legacy stores whole JSON documents as strings inside the report."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _report_measures(folder: str, rid: str, guid: str, ws_name: str,
                     index: Optional[ModelIndex], g: Emitter) -> None:
    """Measures defined in the report rather than the model - a definition that lives one
    layer further from governance, and a common source of conflicts."""
    entities: List[Dict] = []
    ext = read_json(os.path.join(folder, "definition", "reportExtensions.json"), {}) or {}
    entities.extend(ext.get("entities") or [])
    legacy = read_json(os.path.join(folder, "report.json"), {}) or {}
    config = _maybe_json(legacy.get("config"))
    for extension in (config.get("modelExtensions") or []) if isinstance(config, dict) else []:
        entities.extend(extension.get("entities") or [])

    for entity in entities:
        tname = entity.get("name") or "Report"
        for mea in entity.get("measures") or []:
            mname = mea.get("name")
            if not mname:
                continue
            expr = mea.get("expression") or ""
            if isinstance(expr, list):
                expr = "\n".join(str(line) for line in expr)
            mid = node_id("report_measure", guid, tname, mname)
            tid_term = term_id(mname)
            g.node(mid, "report_measure", mname, workspace=ws_name, item_id=guid,
                   parent_id=rid, description=mea.get("description"), expression=expr,
                   expression_norm=norm_dax(expr), table=tname,
                   exact_term=is_exact_term(mname, tid_term))
            g.edge(rid, mid, "contains")
            term = node_id("term", tid_term)
            g.node(term, "term", tid_term.replace("-", " "), aliases=[mname])
            g.edge(mid, term, "defines")
            if index:
                for ref, count in extract_dax_refs(expr, index).items():
                    g.edge(mid, ref, "references", weight=count, via="dax")
