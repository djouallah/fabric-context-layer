"""python -m ask [--json] [--db PATH] <subcommand> ...

Two commands, and that is the whole client. The harvest publishes the ranked context into a
lakehouse of its own and renders it as one markdown file; this fetches that file, and runs
DAX when a question needs a number.

    context [--print]                 fetch Files/context.md - the whole graph, as markdown
    dax <ws-guid>/<model-guid> "<EVALUATE ...>" [--max-rows N]
    evals generate|run|report         the benchmark (maintainer tool; see ask/evals.py)

Read `context.md` for terms, their ranked definitions and DAX, models and their ids, column
values, lineage and who writes what. Take the model's ids from its section and pass them to
`dax`. There is no SQL and no metadata query: a table no semantic model covers has no
agreed definition behind it, and saying so is the answer.

`--json` and `--db` are global and go BEFORE the subcommand.

Exit codes: 0 ok, 2 not found, 3 refused (not a read-only DAX query), 4 Fabric said no.
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
    widths = {c: min(40, max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)))
              for c in columns}
    print("  " + " | ".join(str(c)[:40].ljust(widths[c]) for c in columns))
    print("  " + "-+-".join("-" * widths[c] for c in columns))
    for r in rows[:limit] if limit else rows:
        print("  " + " | ".join(str(r.get(c, ""))[:40].ljust(widths[c]) for c in columns))


def _p_context(c):
    if c.get("text") is not None:
        print(c["text"])
        return
    print(c["path"])
    print("  " + str(round(c["bytes"] / 1024.0, 1)) + " KB, fetched " + str(c["fetched_at"]))
    print("  read it for terms, models, lineage and filter values; `dax` runs the numbers")


def _p_result(r):
    print(r["query"].strip())
    print("  " + str(r["row_count"]) + " rows" + (" (truncated)" if r["truncated"] else "")
          + "  " + str(r["elapsed_ms"]) + " ms")
    _rows(r["rows"], r["columns"])


# ---------------------------------------------------------------- commands

def cmd_context(args, _con):
    out = ctx.context_file(args.db, refresh=args.refresh)
    if args.print or args.json:
        with open(out["path"], encoding="utf-8") as handle:
            out["text"] = handle.read()
    _emit(args, out, _p_context)


def cmd_dax(args, _con):
    """The model is named by its ids, which `context.md` prints on every model section -
    so running a number opens nothing and looks nothing up."""
    workspace_id, dataset_id = ctx.lakehouse_ids(args.model)
    result = fabric.dax(workspace_id, dataset_id, args.query, max_rows=args.max_rows)
    _emit(args, result, _p_result)


def cmd_evals(args, con):
    from . import evals

    evals.main(args, con)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ask", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None,
                        help="where the context was published: the abfss:// URL "
                             "fabcontext.harvest() returned, <ws-guid>/<lakehouse-guid>, or "
                             "a local Tables folder")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch, ignoring the local copy")
    parser.add_argument("--no-cache", action="store_true",
                        help="read the lakehouse directly and keep no local copy (evals)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("context", help="fetch Files/context.md")
    p.add_argument("--print", action="store_true", help="print the file instead of its path")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("dax", help="run DAX on a semantic model")
    p.add_argument("model", help="<workspace-guid>/<model-guid>, from context.md")
    p.add_argument("query", help="EVALUATE ... or DEFINE ...")
    p.add_argument("--max-rows", type=int, default=fabric.MAX_ROWS_DEFAULT)
    p.set_defaults(func=cmd_dax)

    p = sub.add_parser("evals", help="generate or run the benchmark")
    p.add_argument("action", choices=["generate", "run", "report"])
    p.add_argument("--questions", default=None,
                   help="questions file (default ask/evals/questions.yaml)")
    p.add_argument("--condition", choices=["baseline", "treatment", "both"], default="both")
    p.add_argument("--only", action="append", default=[], help="question id; repeatable")
    p.add_argument("--per-class", type=int, default=3)
    p.add_argument("--dry-run", action="store_true",
                   help="validate golden queries, run nothing")
    p.add_argument("--model", default="sonnet", help="Claude model for the headless runs")
    p.add_argument("--budget", type=float, default=1.0, help="max USD per question")
    p.add_argument("--results", default=None, help="results file for 'report'")
    p.set_defaults(func=cmd_evals)

    args = parser.parse_args(argv)
    con = None
    try:
        # Only the benchmark opens the published tables. The client reads one markdown file
        # and calls one REST API, and pays for neither a driver nor a download.
        if args.cmd == "evals":
            from . import db

            args.db = args.db or db.default_db()
            con = db.open_db(args.db, refresh=args.refresh, cache=not args.no_cache)
        elif args.cmd == "context":
            args.db = args.db or ctx.default_db()
    except ctx.NotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:            # noqa: BLE001 - OneLake said no, in its own words
        print("cannot open the context at " + str(args.db) + ": "
              + exc.__class__.__name__ + ": " + str(exc)[:400], file=sys.stderr)
        return 4
    try:
        args.func(args, con)
        return 0
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
        if con is not None:
            con.close()


if __name__ == "__main__":
    sys.exit(main())
