"""Shared helpers: paths, id/slug construction, term normalisation, JSON IO.

No network, no duckdb - everything here is a pure function, so the parse step can be
exercised on a folder of raw JSON without a tenant.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, Iterator, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))     # where schema.sql and the template live

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Characters Windows, git and Obsidian all tolerate in a file name.
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_WS = re.compile(r"\s+")


def slugify(name: str) -> str:
    """A file-name-safe, human-readable slug. Case is kept - these become wiki page names."""
    text = unicodedata.normalize("NFKD", str(name or "unnamed"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _UNSAFE.sub("-", text)
    text = _WS.sub("-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return text or "unnamed"


def page_slug(name: str, guid: str) -> str:
    """Sales-Model--3f2a9c1b - unique tenant-wide, so Obsidian's folder-agnostic [[name]]
    resolution never collides on a duplicated display name."""
    return "{}--{}".format(slugify(name), (guid or "")[:8])


# ---------------------------------------------------------------- node ids

def node_id(kind: str, *parts: str) -> str:
    """kind:part/part - every id carries the owning item's GUID, so display-name collisions
    across workspaces are impossible."""
    return kind + ":" + "/".join(str(p) for p in parts)


def unresolved_id(kind: str, text: str) -> str:
    """A placeholder node for a reference we could not bind to a harvested item. Edges point
    here rather than being dropped, so the misses are countable."""
    return node_id("unresolved", kind, str(text).strip().lower()[:200])


# ---------------------------------------------------------------- terms

# Tokens dropped when normalising a measure name to a term. "Total Revenue", "Sum of
# Revenue" and "Revenue Amount" are the same business term.
_STOP = {"total", "sum", "of", "the", "amount", "nb", "num", "number", "all",
         "a", "an", "measure", "kpi", "value", "values"}
# Spellings folded into one token before anything else is decided, so "Average Price",
# "Avg Price" and "Price_AVG" are one term. Generic English only - no domain words.
_SYNONYM = {"average": "avg", "mean": "avg", "maximum": "max", "minimum": "min",
            "percent": "pct", "percentage": "pct", "cnt": "count", "qty": "quantity"}
# Tokens that CHANGE the term and must survive normalisation.
_KEEP = {"ytd", "mtd", "qtd", "wtd", "ly", "py", "yoy", "mom", "pct", "avg",
         "min", "max", "net", "gross", "budget", "forecast", "actual", "target", "count"}
# Plurals that are the business word itself - stripping the 's' would invent a term.
_KEEP_PLURAL = {"sales", "expenses", "goods", "analytics", "logistics", "statistics",
                "receivables", "payables", "earnings", "savings", "proceeds"}

# Hand-written merges the word lists cannot make ("Net Sales" is "revenue"), as
# {term_id: [spelling, ...]}. Passed in by the caller, or read from an optional aliases.yaml
# sitting beside this file:
#     revenue: [Net Sales, Turnover]
ALIAS_FILE = os.path.join(HERE, "aliases.yaml")
_FILE_ALIASES: Optional[Dict[str, List[str]]] = None
_GIVEN: Dict[str, List[str]] = {}
_OVERRIDES: Optional[Dict[str, str]] = None


def _raw_tokens(name: str) -> List[str]:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("%", " pct ").replace("#", " ")
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if t]


def term_tokens(name: str) -> List[str]:
    """The normalised tokens of a measure name, SORTED so word order cannot split a term:
    synonyms folded, stop words dropped, trailing plural 's' removed."""
    tokens = _raw_tokens(name)
    out: List[str] = []
    for tok in tokens:
        tok = _SYNONYM.get(tok, tok)
        if tok in _KEEP:
            out.append(tok)
            continue
        if tok in _STOP:
            continue
        if (len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss")
                and tok not in _KEEP_PLURAL):
            tok = tok[:-1]
        out.append(tok)
    if not out:                       # a name made only of stop words is its own term
        out = tokens or ["unnamed"]
    return sorted(dict.fromkeys(out))


def _normalise(mapping: Dict) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for tid, aliases in (mapping or {}).items():
        if isinstance(aliases, str):
            aliases = [aliases]
        out[str(tid)] = [str(a) for a in (aliases or [])]
    return out


def set_aliases(mapping: Optional[Dict[str, List[str]]]) -> None:
    """Merge `mapping` into the hand-written aliases, for this process.

    Called before parsing, not after: a term id is decided the moment a measure name is
    normalised, so an alias that arrives later would merge the alias rows without merging the
    terms themselves.
    """
    global _OVERRIDES
    _GIVEN.clear()
    _GIVEN.update(_normalise(mapping or {}))
    _OVERRIDES = None


def manual_aliases(extra: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[str]]:
    """{term id: [alias, ...]} - what `set_aliases` was given, merged over aliases.yaml when
    one is present. `{}` when there is neither."""
    global _FILE_ALIASES
    if extra is not None:
        set_aliases(extra)
    if _FILE_ALIASES is None:
        _FILE_ALIASES = {}
        if os.path.exists(ALIAS_FILE):
            try:
                import yaml
                with open(ALIAS_FILE, "r", encoding="utf-8") as fh:
                    _FILE_ALIASES = _normalise(yaml.safe_load(fh) or {})
            except Exception as exc:                     # noqa: BLE001 - optional file
                print("[warn] " + ALIAS_FILE + ": " + str(exc)[:120])
    if not _GIVEN:
        return dict(_FILE_ALIASES)
    merged = dict(_FILE_ALIASES)
    for tid, names in _GIVEN.items():
        merged[tid] = list(dict.fromkeys(list(merged.get(tid, [])) + names))
    return merged


def _overrides() -> Dict[str, str]:
    global _OVERRIDES
    if _OVERRIDES is None:
        _OVERRIDES = {}
        for tid, aliases in manual_aliases().items():
            for alias in aliases:
                _OVERRIDES["-".join(term_tokens(alias))] = tid
                _OVERRIDES[" ".join(_raw_tokens(alias))] = tid
    return _OVERRIDES


def term_id(name: str) -> str:
    """A business term id from a measure/column name: the sorted normalised tokens joined
    with '-', unless src/aliases.yaml pins the name to another term."""
    over = _overrides()
    if over:
        hit = over.get(" ".join(_raw_tokens(name)))
        if hit:
            return hit
    key = "-".join(term_tokens(name))
    return over.get(key, key) if over else key


_ACRONYMS = {"ytd", "mtd", "qtd", "wtd", "ly", "py", "yoy", "mom", "pct", "kpi"}


def term_label(tid: str) -> str:
    """A term id rendered as a title when nothing better is known: 'revenue-ytd' ->
    'Revenue YTD'. The wiki prefers the top-ranked measure's own name (terms.label)."""
    return " ".join(t.upper() if t in _ACRONYMS else t.capitalize()
                    for t in str(tid).split("-"))


def is_exact_term(name: str, tid: str) -> bool:
    """Whether the name normalises to the term with nothing dropped - a relevance signal.
    Synonym folding and word order do not count as a change."""
    plain = sorted(dict.fromkeys(_SYNONYM.get(t, t) for t in _raw_tokens(name)))
    return "-".join(plain) == tid


# ---------------------------------------------------------------- DAX

_DAX_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_DAX_COMMENT_LINE = re.compile(r"(?://|--)[^\n]*")
_DAX_STRING = re.compile(r'"(?:[^"\\]|\\.|"")*"')


def strip_dax(expr: str) -> str:
    """DAX with comments and string literals blanked out, so a reference regex cannot match
    a bracketed name inside a comment or a quoted string."""
    text = _DAX_COMMENT_BLOCK.sub(" ", str(expr or ""))
    text = _DAX_COMMENT_LINE.sub(" ", text)
    return _DAX_STRING.sub('""', text)


def norm_dax(expr: str) -> str:
    """A DAX expression reduced to a comparison key: comments gone, whitespace collapsed,
    lower-cased. Two measures with the same key are the same definition."""
    text = strip_dax(expr)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def m_text(expression: Any) -> str:
    """TMSL writes an M expression as a string or as a list of lines."""
    if isinstance(expression, list):
        return "\n".join(str(line) for line in expression)
    return "" if expression is None else str(expression)


# ---------------------------------------------------------------- JSON IO

def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return default


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False, default=str)


def read_text(path: str, default: str = "") -> str:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class JsonlWriter:
    """Append-only JSONL sink that de-duplicates on a key, so a node or edge emitted by two
    parsers lands once (the second occurrence merges its weight and fills blanks)."""

    def __init__(self, path: str, key: Iterable[str]):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.key = tuple(key)
        self._rows: Dict[tuple, Dict] = {}

    def add(self, row: Dict) -> None:
        k = tuple(row.get(f) for f in self.key)
        prev = self._rows.get(k)
        if prev is None:
            self._rows[k] = row
            return
        if "weight" in row and "weight" in prev:
            prev["weight"] = (prev.get("weight") or 0) + (row.get("weight") or 0)
        for field, value in row.items():
            if field in ("weight", "attrs"):
                continue
            if prev.get(field) in (None, "", {}, []) and value not in (None, "", {}, []):
                prev[field] = value
        if isinstance(prev.get("attrs"), dict) and isinstance(row.get("attrs"), dict):
            for field, value in row["attrs"].items():
                if prev["attrs"].get(field) in (None, "", {}, []):
                    prev["attrs"][field] = value

    def rows(self) -> List[Dict]:
        return list(self._rows.values())

    def flush(self) -> int:
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            for row in self._rows.values():
                out = dict(row)
                if isinstance(out.get("attrs"), dict):
                    out["attrs"] = json.dumps(out["attrs"], ensure_ascii=False, default=str)
                fh.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
        return len(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def walk_files(root: str, name: Optional[str] = None) -> Iterator[str]:
    for base, _dirs, files in os.walk(root):
        for fname in files:
            if name is None or fname == name:
                yield os.path.join(base, fname)


def iso(value: Any) -> Optional[str]:
    """Fabric hands back several date spellings; keep whatever is parseable as text and let
    DuckDB cast it."""
    if not value:
        return None
    text = str(value).replace("Z", "")
    return text[:26]


class Emitter:
    """Collects nodes and edges from every parser, then writes the two JSONL files.

    Nodes are keyed on id and edges on (src, rel, dst), so the same fact discovered twice -
    once from the scanner, once from a definition file - lands once.
    """

    def __init__(self, build_dir: str):
        self.nodes = JsonlWriter(os.path.join(build_dir, "nodes.jsonl"), ["id"])
        self.edges = JsonlWriter(os.path.join(build_dir, "edges.jsonl"), ["src", "rel", "dst"])
        self.activity = JsonlWriter(os.path.join(build_dir, "activity.jsonl"), ["event_id"])
        self.query_usage = JsonlWriter(os.path.join(build_dir, "query_usage.jsonl"), ["def_id"])
        self.query_stats = JsonlWriter(os.path.join(build_dir, "query_stats.jsonl"), ["item_id"])

    def node(self, nid, kind, name, workspace=None, item_id=None, parent_id=None,
             description=None, endorsement=None, owner=None, modified_at=None, **attrs):
        self.nodes.add({
            "id": nid, "kind": kind, "name": str(name), "workspace": workspace,
            "item_id": item_id, "parent_id": parent_id, "description": description,
            "endorsement": endorsement, "owner": owner, "modified_at": modified_at,
            "attrs": {k: v for k, v in attrs.items() if v not in (None, "", [], {})},
        })
        return nid

    def edge(self, src, dst, rel, weight=1.0, **attrs):
        if not src or not dst or src == dst:
            return
        self.edges.add({"src": src, "dst": dst, "rel": rel, "weight": float(weight),
                        "attrs": {k: v for k, v in attrs.items() if v not in (None, "", [], {})}})

    def event(self, event_id, **row):
        self.activity.add(dict(row, event_id=event_id))

    def queried(self, def_id, **row):
        """Per-definition counts from the DAX that actually ran. Counts only - the query
        text stays in raw/ and is never published."""
        self.query_usage.add(dict(row, def_id=def_id))

    def query_stat(self, item_id, **row):
        """One row per semantic model in a workspace whose query log was read, so a model
        with no queries is distinguishable from a model nobody monitored."""
        self.query_stats.add(dict(row, item_id=item_id))

    def flush(self):
        return {"nodes": self.nodes.flush(), "edges": self.edges.flush(),
                "activity": self.activity.flush(),
                "query_usage": self.query_usage.flush(),
                "query_stats": self.query_stats.flush()}
