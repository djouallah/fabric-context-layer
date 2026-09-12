"""python -m ask [--json] [--db PATH] <subcommand> ...

The harvest side publishes the context into a Fabric lakehouse of its own and records the
address the harvest returned; pass it with --db. This pulls the tables into
memory and answers from them. Numbers come from DAX on the model, never from here.
--db overrides the address. --json is global and goes BEFORE the subcommand.

    contract                          what the published database honours
    scope                             build time, workspaces, models, stores, counts
    search "<words>" [--kind K]       ranked hits across terms, measures, tables, columns
    define "<term>"                   every definition, ranked, with the DAX and the model
    model <name|id> [--table T] [--brief]
    table <store.schema.table|id>
    lineage <node|name> [--down] [--depth N]
    usage <term|node>
    values <model> <table> <column>   distinct values and range; harvested, else live DAX
    dax <model> "<EVALUATE ...>" [--max-rows N]
    sql "<select ...>" [--max-rows N]   DuckDB SQL over the context tables themselves
    evals generate|run ...            the benchmark (see ask/evals.py)

Exit codes: 0 ok, 2 not found or ambiguous, 3 refused (not a read-only query), 4 Fabric
said no.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import context as ctx
from . import fabric

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------- printing

def _emit(args, payload, text_fn) -> None:
    if args.json:
        print(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    else:
        text_fn(payload)


def _rows(rows, columns, limit=None):
    if not rows:
        print("  (no rows)")
        return
    widths = {c: min(40, max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows))) for c in columns}
    line = "  " + " | ".join(str(c)[:40].ljust(widths[c]) for c in columns)
    print(line)
    print("  " + "-+-".join("-" * widths[c] for c in columns))
    for r in rows[:limit] if limit else rows:
        print("  " + " | ".join(str(r.get(c, ""))[:40].ljust(widths[c]) for c in columns))


def _p_contract(c):
    print(("ok" if c["ok"] else "INCOMPLETE") + "  " + str(c["db"]))
    if c.get("lakehouse"):
        print("  lakehouse: " + str(c["lakehouse"]) + " in " + str(c["workspace"])
              + "   published_at: " + str(c["published_at"]))
    if c.get("local_copy"):
        print("  local copy: " + str(c["local_copy"]) + "  (--refresh to re-pull)")
    print("  built_at: " + str(c["built_at"]) + "  schema_version: " + str(c["schema_version"])
          + "  profiled columns: " + str(c["profiled_columns"]))
    for label, spec in (("required", c["required"]), ("optional", c["optional"])):
        print("  " + label + ":")
        for table, st in spec.items():
            state = "present" if st["present"] else "MISSING"
            if st["missing_columns"]:
                state += ", missing columns " + ", ".join(st["missing_columns"])
            print("    " + table.ljust(14) + state)


def _p_scope(s):
    print("built_at: " + str(s["built_at"]) + "   terms: " + str(s["terms"]["total"])
          + " (" + str(s["terms"]["conflicting"]) + " conflicting)")
    print("workspaces: " + ", ".join(w["name"] + " (" + str(w.get("id")) + ")" for w in s["workspaces"]))
    print("models (execute with workspace_id + dataset_id):")
    _rows(s["models"], ["name", "workspace", "item_id", "workspace_id", "n_measures", "n_tables",
                        "endorsement"])
    if s.get("empty_models"):
        print("  (" + str(s["empty_models"]) + " empty auto-created models not listed: "
              "no measures, no tables, nothing to query)")
    print("stores (n_used: tables a model, notebook or pipeline touches):")
    _rows(s["stores"], ["name", "kind", "workspace", "item_id", "n_used", "n_tables",
                        "external"])
    print("counts: " + ", ".join(k + "=" + str(v) for k, v in s["counts"].items()))
    print("optional tables: " + ", ".join(k + ("" if v else " (missing)")
                                          for k, v in s["optional_tables"].items()))


def _p_search(s):
    if not s["hits"]:
        print("nothing above the threshold for " + repr(s["query"]))
        return
    _rows(s["hits"], ["score", "kind", "name", "parent", "workspace", "tier", "term_id",
                      "why", "id"])


def _p_define(d):
    print(d["label"] + "  (term " + d["term_id"] + ")  " + d["note"])
    if d["aliases"]:
        print("  also known as: " + ", ".join(d["aliases"]))
    for x in d["definitions"]:
        print("\n  #" + str(x["rank"]) + "  " + x["name"] + "  in " + str(x["model"]["name"])
              + " [" + str(x["model"]["workspace"]) + "]  score " + str(x["score"])
              + "  " + json.dumps(x["signals"])
              + ("  " + str(x["endorsement"]) if x["endorsement"] else ""))
        if x.get("description"):
            print("     " + x["description"])
        q = x.get("queried")
        if q:
            print("     queried " + str(q.get("queries", 0)) + "x in 28d ("
                  + str(q.get("adhoc_queries", 0)) + " hand-written), "
                  + str(q.get("query_users", 0)) + "+ users, last "
                  + str(q.get("last_queried") or "never"))
        for line in (x["expression"] or "(no expression)").strip().splitlines():
            print("     | " + line)
        print("     execute: dataset " + str(x["model"]["item_id"]) + " in workspace "
              + str(x["model"]["workspace_id"]) + "; measure [" + x["name"] + "]"
              + ("; used by " + ", ".join(r["name"] for r in x["reports"]) if x["reports"] else ""))


def _p_model(m):
    print(m["name"] + "  [" + str(m["workspace"]) + "]  dataset " + str(m["item_id"])
          + "  workspace " + str(m["workspace_id"]) + "  " + str(m.get("storage_mode") or "")
          + ("  " + str(m["endorsement"]) if m.get("endorsement") else ""))
    if m.get("usage"):
        print("  28-day usage: " + json.dumps(m["usage"]))
    for t in m["tables"]:
        src = t.get("source") or []
        print("\n  table " + t["name"] + ("  (" + str(t["mode"]) + ")" if t.get("mode") else "")
              + ("  rows " + format(int(t["n_rows"]), ",") if t.get("n_rows") else "")
              + ("  <- " + ", ".join(str(s["store"]) + "." + str(s["schema"]) + "." + str(s["table"])
                                     + (" [external]" if s.get("external") else "") for s in src)
                 if src else ""))
        cols = t.get("columns") or []
        if cols and isinstance(cols[0], str):
            print("    columns: " + ", ".join(cols))
        else:
            for c in cols:
                prof = c.get("profile") or {}
                extra = ""
                if prof.get("values"):
                    extra = "  values: " + ", ".join(str(v) for v in prof["values"][:12]) \
                            + (" ..." if len(prof["values"]) > 12 else "")
                elif "min" in prof or "max" in prof:
                    extra = "  range: " + str(prof.get("min")) + " .. " + str(prof.get("max"))
                print("    " + c["name"] + "  " + str(c.get("data_type") or "")
                      + ("  hidden" if c.get("is_hidden") else "") + extra)
    if m["relationships"]:
        print("\n  relationships:")
        for r in m["relationships"]:
            print("    " + r["from_table"] + "[" + str(r["from_column"]) + "] -> "
                  + r["to_table"] + "[" + str(r["to_column"]) + "]"
                  + ("" if r["is_active"] else "  (inactive)")
                  + ("  " + str(r["cross_filter"]) if r.get("cross_filter") else ""))
    if m["measures"]:
        print("\n  measures:")
        for x in m["measures"]:
            print("    [" + x["name"] + "]  table " + str(x["table"]) + "  term " + str(x["term_id"])
                  + " rank " + str(x["rank"]) + (" CONFLICT" if x["conflicting"] else ""))
            if x.get("description"):
                print("      " + x["description"])
            for line in (x["expression"] or "").strip().splitlines():
                print("      | " + line)


def _p_table(t):
    print(str(t["store"]) + "." + str(t["schema"]) + "." + t["name"] + "  [" + str(t["workspace"])
          + "]  lakehouse " + str(t["lakehouse_id"]) + "  workspace " + str(t["workspace_id"])
          + ("  EXTERNAL (not harvested)" if t["external"] else "")
          + ("  rows " + format(int(t["n_rows"]), ",") if t.get("n_rows") is not None else ""))
    if t.get("columns"):
        _rows(t["columns"], ["name", "type", "n_distinct", "min", "max", "values"])
    else:
        print("  columns: not profiled (python src/run.py profile on the harvest side)")
    for label in ("written_by", "read_by"):
        if t[label]:
            print("  " + label.replace("_", " ") + ": " + ", ".join(
                n["name"] + " (" + n["kind"] + ")" for n in t[label]))
    if t["models_using"]:
        print("  models: " + ", ".join(x["model"] + " / " + x["model_table"] for x in t["models_using"]))


def _p_lineage(l):
    print(("upstream" if l["direction"] == "up" else "downstream") + " of " + l["node"])
    for n in l["nodes"]:
        print("  " * n["depth"] + str(n["depth"]) + ". " + n["kind"] + "  " + str(n["name"])
              + ("  [" + str(n["workspace"]) + "]" if n.get("workspace") else "")
              + ("  EXTERNAL, not harvested" if n.get("external") else "")
              + "  (" + str(n["rel"]) + ")")


def _p_usage(u):
    print("usage of " + u["node"])
    for label in ("reports", "referenced_by", "mentioned_in", "downstream"):
        if u[label]:
            print("  " + label.replace("_", " ") + ":")
            for n in u[label]:
                print("    " + str(n.get("kind", "report")) + "  " + str(n["name"])
                      + ("  views " + str(n["views"]) if "views" in n else "")
                      + ("  visuals " + str(n["visuals"]) if "visuals" in n else ""))
    if u.get("owner_activity_28d"):
        print("  owner activity (28d): " + json.dumps(u["owner_activity_28d"]))


def _p_values(v):
    print(str(v["table"]) + "[" + str(v["column"]) + "]  source: " + v["source"])
    if v.get("values"):
        print("  values: " + ", ".join(str(x) for x in v["values"]) + (" ..." if v.get("truncated") else ""))
    for k in ("min", "max", "n_distinct", "null_frac"):
        if v.get(k) is not None:
            print("  " + k + ": " + str(v[k]))


def _p_result(r):
    print(r["query"].strip())
    print("  " + str(r["row_count"]) + " rows" + (" (truncated)" if r["truncated"] else "")
          + "  " + str(r["elapsed_ms"]) + " ms"
          + ("  via SQL endpoint: " + ", ".join(r["attached"]) if r.get("attached") else ""))
    _rows(r["rows"], r["columns"])


# ---------------------------------------------------------------- commands

def cmd_contract(args, con):
    _emit(args, ctx.contract(con, args.db), _p_contract)


def cmd_scope(args, con):
    _emit(args, ctx.scope(con, args.db), _p_scope)


def cmd_search(args, con):
    _emit(args, ctx.search(con, args.text, limit=args.limit, kinds=args.kind or None), _p_search)


def cmd_define(args, con):
    _emit(args, ctx.define(con, args.term), _p_define)


def cmd_model(args, con):
    _emit(args, ctx.model(con, args.model, table=args.table, brief=args.brief), _p_model)


def cmd_table(args, con):
    _emit(args, ctx.table(con, args.table), _p_table)


def cmd_lineage(args, con):
    _emit(args, ctx.lineage(con, args.node, "down" if args.down else "up", args.depth), _p_lineage)


def cmd_usage(args, con):
    _emit(args, ctx.usage(con, args.node), _p_usage)


def cmd_values(args, con):
    prof = ctx.column_profile(con, args.model, args.table, args.column)
    if prof and not args.live:
        _emit(args, prof, _p_values)
        return
    m = ctx.resolve_model(con, args.model)
    _emit(args, fabric.values(m["workspace_id"], m["item_id"], args.table, args.column,
                              limit=args.limit), _p_values)


def cmd_dax(args, con):
    m = ctx.resolve_model(con, args.model)
    result = fabric.dax(m["workspace_id"], m["item_id"], args.query, max_rows=args.max_rows)
    result["model"] = m["name"]
    _emit(args, result, _p_result)


def cmd_sql(args, con):
    result = ctx.raw_sql(con, args.query, max_rows=args.max_rows,
                         attach_stores=args.store)
    _emit(args, result, _p_result)


def cmd_evals(args, con):
    from . import evals
    evals.main(args, con)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ask", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None,
                        help="where the context was published: "
                             "<workspace>/<lakehouse>.Lakehouse, <ws-guid>/<lh-guid>, an "
                             "abfss:// URL, or a local folder. Default: context.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--refresh", action="store_true",
                        help="re-pull the context from the lakehouse, ignoring the local copy")
    parser.add_argument("--no-cache", action="store_true",
                        help="read the lakehouse directly and keep no local copy (slow)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("contract").set_defaults(func=cmd_contract)
    sub.add_parser("scope").set_defaults(func=cmd_scope)
    p = sub.add_parser("search")
    p.add_argument("text")
    p.add_argument("--kind", action="append", help="restrict to a node kind; repeatable")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("define")
    p.add_argument("term")
    p.set_defaults(func=cmd_define)
    p = sub.add_parser("model")
    p.add_argument("model")
    p.add_argument("--table")
    p.add_argument("--brief", action="store_true", help="column names only")
    p.set_defaults(func=cmd_model)
    p = sub.add_parser("table")
    p.add_argument("table")
    p.set_defaults(func=cmd_table)
    p = sub.add_parser("lineage")
    p.add_argument("node")
    p.add_argument("--down", action="store_true")
    p.add_argument("--depth", type=int, default=12)
    p.set_defaults(func=cmd_lineage)
    p = sub.add_parser("usage")
    p.add_argument("node")
    p.set_defaults(func=cmd_usage)
    p = sub.add_parser("values")
    p.add_argument("model")
    p.add_argument("table")
    p.add_argument("column")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--live", action="store_true", help="ask the model even when a profile exists")
    p.set_defaults(func=cmd_values)
    p = sub.add_parser("dax")
    p.add_argument("model")
    p.add_argument("query")
    p.add_argument("--max-rows", type=int, default=fabric.MAX_ROWS_DEFAULT)
    p.set_defaults(func=cmd_dax)
    p = sub.add_parser("sql", help="DuckDB SQL over the context tables, and over a "
                                   "store's SQL endpoint when the query names one")
    p.add_argument("query", help="SELECT over nodes, edges, terms, definitions, aliases, "
                                 "... and over <store>.<schema>.<table> for data no "
                                 "semantic model covers")
    p.add_argument("--store", action="append", default=[],
                   help="attach this lakehouse or warehouse even if the query does not "
                        "name it; repeatable")
    p.add_argument("--max-rows", type=int, default=fabric.MAX_ROWS_DEFAULT)
    p.set_defaults(func=cmd_sql)
    p = sub.add_parser("evals", help="generate or run the benchmark")
    p.add_argument("action", choices=["generate", "run", "report"])
    p.add_argument("--questions", default=None, help="questions file (default ask/evals/questions.yaml)")
    p.add_argument("--condition", choices=["baseline", "treatment", "both"], default="both")
    p.add_argument("--only", action="append", default=[], help="question id; repeatable")
    p.add_argument("--per-class", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="validate golden queries, run nothing")
    p.add_argument("--model", default="sonnet", help="Claude model for the headless runs")
    p.add_argument("--budget", type=float, default=1.0, help="max USD per question")
    p.add_argument("--results", default=None, help="results file for 'report'")
    p.set_defaults(func=cmd_evals)

    args = parser.parse_args(argv)
    try:
        args.db = args.db or ctx.default_db()
        con = ctx.open_db(args.db, refresh=args.refresh, cache=not args.no_cache)
    except ctx.NotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:                   # noqa: BLE001 - OneLake said no, in its own words
        print("cannot open the context at " + str(args.db) + ": "
              + exc.__class__.__name__ + ": " + str(exc)[:400], file=sys.stderr)
        return 4
    try:
        args.func(args, con)
        return 0
    except ctx.Ambiguous as exc:
        print(str(exc), file=sys.stderr)
        if args.json:
            print(json.dumps({"error": "ambiguous", "candidates": exc.candidates}, default=str))
        return 2
    except ctx.NotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except fabric.Refused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 3
    except fabric.RemoteError as exc:
        print("fabric: " + str(exc), file=sys.stderr)
        return 4
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
