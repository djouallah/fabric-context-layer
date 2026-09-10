"""CLI.

    python src/run.py harvest --to "Sales/context_layer" --workspace "Sales" [--days 28]
    python src/run.py harvest --workspace "Sales" --query-log   # + the DAX that actually ran
    python src/run.py build                 # raw/ -> build/*.jsonl -> the lakehouse
    python src/run.py profile [--store X]   # lakehouse columns, stats and values
    python src/run.py wiki                  # the context -> wiki/
    python src/run.py viz                   # the context -> graph.html (one file, no server)
    python src/run.py all --to "Sales/context_layer" --workspace "Sales"
    python src/run.py files push|pull|list  # the working files <-> the lakehouse Files/
    python src/run.py deploy --daily 03:00 --tz "AUS Eastern Standard Time"   # nightly refresh
    python src/run.py query "select * from terms where conflicting"
    python src/run.py lineage "term:revenue" [--down] [--depth 12]
    python src/run.py check                 # verification counts + broken wikilinks

The repo holds Python and no data. Everything the harvest reads or writes lives in one
Fabric lakehouse: the ranked graph as Delta tables under Tables/, and raw/, build/, wiki/
and graph.html under Files/. `--to <workspace>/<lakehouse>` names it once; context.json
remembers it after that. Work happens in a folder outside the repo, printed by every step,
and each step pushes what it produced (`--no-push` to keep it local for now).
"""
from __future__ import annotations

import argparse
import os
import sys

import common

# DuckDB prints result tables with box-drawing characters; the Windows console defaults to
# cp1252 and would raise on them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _target(args) -> str:
    """The lakehouse this context lives in, as `<workspace>/<lakehouse>`."""
    import publish
    to = getattr(args, "to", None)
    if to:
        ws, _sep, lh = to.partition("/")
        if not ws or not lh:
            raise SystemExit("--to needs <workspace>/<lakehouse> (display names)")
        return to
    where = publish.load_location()
    if not where:
        raise SystemExit('no lakehouse yet; name one with --to "<workspace>/<lakehouse>"')
    return where["workspace"] + "/" + where["lakehouse"]


def _store(args) -> str:
    """The same lakehouse, in the form duckrun resolves to a OneLake URL. `<ws>/<lh>` on
    its own is a LOCAL relative path to duckrun; only `<ws>/<lh>.Lakehouse` and a pair of
    GUIDs are shorthand, so publish's recorded GUID path is preferred."""
    import publish
    where = publish.load_location()
    target = _target(args)
    if where and where["workspace"] + "/" + where["lakehouse"] == target:
        return where["path"]
    ws, _sep, lh = target.partition("/")
    return ws + "/" + lh + (".Lakehouse" if not lh.lower().endswith(".lakehouse") else "")


def _work(args) -> str:
    """The working folder for this context, outside the repo."""
    work = getattr(args, "work", None) or common.workdir(_target(args))
    for sub in ("raw", "build", "wiki"):
        os.makedirs(os.path.join(work, sub), exist_ok=True)
    return work


def _paths(args):
    """(work, raw, build, wiki, graph.html) for this run."""
    work = _work(args)
    return (work, os.path.join(work, "raw"), os.path.join(work, "build"),
            os.path.join(work, "wiki"), os.path.join(work, "graph.html"))


def _push(args, items) -> None:
    """Send what a step just produced up to Files/, unless asked not to."""
    if getattr(args, "no_push", False):
        return
    import files
    out = files.push(_store(args), _work(args), items, log=lambda _m: None)
    for item, st in out.items():
        print("  Files/" + item + ": " + str(st["sent"]) + " sent, " + str(st["deleted"])
              + " deleted, " + str(st["skipped"]) + " unchanged")


def _open(args):
    """A connection over the published context, read back from the lakehouse."""
    import graph
    return graph.open_published(getattr(args, "context", None))


def _harvest(args) -> None:
    import harvest
    if not args.workspace:
        raise SystemExit("harvest needs at least one --workspace")
    _work_dir, raw, _b, _w, _g = _paths(args)
    print("harvesting into " + raw)
    harvest.RAW = raw
    harvest.harvest(args.workspace, days=args.days, refresh=args.refresh,
                    scanner=not args.no_scanner, activity=not args.no_activity,
                    query_log=args.query_log)
    _push(args, ["raw"])


def _build(args):
    """Parse raw/ into the graph, derive the ranked layer, publish it. Returns the
    in-memory connection so a chained step can use it without a round trip."""
    import graph
    import parse
    import publish
    work, raw, build_dir, _w, _g = _paths(args)
    ws, _sep, lh = _target(args).partition("/")
    print("parsing " + raw + " ...")
    parse.build(raw, build_dir)
    print("building the graph ...")
    con, counts = graph.build(build_dir)
    print("  " + ", ".join(k + "=" + str(v) for k, v in sorted(counts.items())))
    print("publishing into lakehouse " + lh + " in workspace " + ws + " ...")
    out = publish.publish_lakehouse(con, ws, lh, work=work, folder=args.folder or None)
    print(("  created " if out["created"] else "  updated ") + out["lakehouse"] + " ("
          + out["lakehouse_id"] + "): "
          + ", ".join(k + "=" + str(v) for k, v in out["tables"].items()))
    print("  " + out["url"])
    print("  recorded in " + os.path.relpath(publish.LOCATION))
    _push(args, ["build"])
    return con


def _profile(args, con=None) -> None:
    import profiling
    _work_dir, raw, _b, _w, _g = _paths(args)
    profiling.RAW = raw
    own = con is None
    con = con or _open(args)
    try:
        profiling.run(con, stores=args.store, all_tables=args.all,
                      values=not args.no_values, dry_run=args.dry_run)
    finally:
        if own:
            con.close()
    if not args.dry_run:
        _push(args, ["raw"])


def _wiki(args, con=None) -> None:
    import wiki
    _work_dir, _r, _b, out_dir, _g = _paths(args)
    own = con is None
    con = con or _open(args)
    counts = wiki.render(con, out_dir)
    if own:
        con.close()
    print("wrote " + str(sum(counts.values())) + " pages to " + out_dir + ": "
          + ", ".join(k + "=" + str(v) for k, v in sorted(counts.items())))
    broken = wiki.check_links(out_dir)
    print("broken wikilinks: " + str(len(broken)))
    for page, target in broken[:10]:
        print("  " + page + " -> " + target)
    _push(args, ["wiki"])


def _viz(args, con=None) -> None:
    import shutil
    import viz
    _work_dir, _r, _b, _w, graph_html = _paths(args)
    own = con is None
    con = con or _open(args)
    out = viz.render(con, graph_html)
    if own:
        con.close()
    print("wrote " + out + " (" + str(os.path.getsize(out) // 1024) + " KB)")
    # A second copy inside the repo, for publishing the graph as a static page. It is the
    # one thing the repo holds that IS harvested content, so it is opt-in per run.
    copy_to = getattr(args, "copy_to", None)
    if copy_to:
        dest = copy_to if os.path.isabs(copy_to) else os.path.join(common.ROOT, copy_to)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(out, dest)
        print("copied to " + dest)
    _push(args, ["graph.html"])


def _files(args) -> None:
    import files
    work, target = _work(args), _store(args)
    items = args.item or list(files.ITEMS)
    if args.action == "push":
        out = files.push(target, work, items, full=args.full)
        for item, st in out.items():
            print(item + ": " + str(st["sent"]) + " sent (" + files._human(st["bytes"])
                  + "), " + str(st["deleted"]) + " deleted, " + str(st["skipped"]) + " unchanged")
    elif args.action == "pull":
        print("pulling into " + work)
        files.pull(target, work, items)
    else:
        for item, keys in files.listing(target, items).items():
            print(item + ": " + str(len(keys)) + " files")
            for key in keys[:8]:
                print("    " + key)
            if len(keys) > 8:
                print("    ... " + str(len(keys) - 8) + " more")


def _deploy(args) -> None:
    import deploy as deploy_mod
    import publish
    where = publish.load_location()
    if not where:
        raise SystemExit('nothing published yet; run build --to "<workspace>/<lakehouse>" first')
    workspaces = args.workspace or _harvested_workspaces(args)
    if not workspaces:
        raise SystemExit("nothing to harvest: pass --workspace, or build the context once "
                         "so the notebook can inherit the workspace list from it")
    out = deploy_mod.deploy(where["workspace"], where["lakehouse"], where["path"], workspaces,
                            name=args.name, days=args.days,
                            stale_after_days=args.stale_after_days,
                            profile=not args.no_profile, wiki=not args.no_wiki,
                            query_log=args.query_log,
                            daily=None if args.no_schedule else args.daily, tz=args.tz,
                            folder=args.folder or None, run=args.run)
    print("  harvesting: " + ", ".join(workspaces))
    print("  " + out["url"])


def _harvested_workspaces(args) -> list:
    """The workspaces the published context was built from - so a redeploy keeps harvesting
    the same ones without being told again."""
    import json as _json
    con = _open(args)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'workspaces'").fetchone()
        return [w["name"] for w in _json.loads(row[0])] if row and row[0] else []
    except Exception:                                  # noqa: BLE001 - meta is optional
        return []
    finally:
        con.close()


def _query(args) -> None:
    con = _open(args)
    con.sql(args.sql).show(max_rows=args.limit)
    con.close()


def _lineage(args) -> None:
    import graph
    con = _open(args)
    rows = graph.lineage(con, args.node, "down" if args.down else "up", args.depth)
    if not rows:
        print("nothing " + ("downstream" if args.down else "upstream") + " of " + args.node)
    for nid, kind, name, workspace, depth, rel in rows:
        print("  " * depth + str(depth) + ". " + kind + "  " + name
              + ("  [" + str(workspace) + "]" if workspace else "") + "  (" + str(rel) + ")")
    con.close()


def _check(args) -> None:
    import wiki
    con = _open(args)
    checks = [
        ("nodes by kind", "SELECT kind, count(*) n FROM nodes GROUP BY 1 ORDER BY 2 DESC"),
        ("edges by relation", "SELECT rel, count(*) n, round(avg(weight),1) avg_weight "
                              "FROM edges GROUP BY 1 ORDER BY 2 DESC"),
        # unresolved: targets are deliberately nodeless placeholders, not dangling edges.
        ("dangling edges (want 0)",
         "SELECT count(*) n FROM edges e LEFT JOIN nodes n ON n.id = e.dst "
         "WHERE n.id IS NULL AND e.dst NOT LIKE 'unresolved:%'"),
        ("unresolved references (edges to deliberately nodeless ids)",
         "SELECT split_part(dst, '/', 1) kind, count(*) n, count(DISTINCT dst) distinct_targets "
         "FROM edges WHERE dst LIKE 'unresolved:%' GROUP BY 1 ORDER BY 2 DESC"),
        ("top unresolved targets",
         "SELECT dst, count(*) n FROM edges WHERE dst LIKE 'unresolved:%' "
         "GROUP BY 1 ORDER BY 2 DESC LIMIT 12"),
        ("models with no tables parsed",
         "SELECT count(*) n FROM nodes m WHERE m.kind = 'semantic_model' AND NOT EXISTS "
         "(SELECT 1 FROM edges e WHERE e.src = m.id AND e.rel = 'contains')"),
        ("terms with conflicting definitions",
         "SELECT count(*) n FROM terms WHERE conflicting"),
        ("external stores (bound to, not harvested)",
         "SELECT name, json_extract_string(attrs, '$.via') via, json_extract_string(attrs, '$.workspace_id') workspace_id FROM nodes "
         "WHERE kind = 'lakehouse' AND TRY_CAST(json_extract_string(attrs, '$.external') AS BOOLEAN)"),
        ("aliases per term (top 10)",
         "SELECT term_id, count(*) n, list(alias) FROM aliases GROUP BY 1 ORDER BY 2 DESC LIMIT 10"),
        ("profiled tables and columns",
         "SELECT count(*) FILTER (WHERE kind = 'lakehouse_table' "
         "AND json_extract_string(attrs, '$.profiled_at') IS NOT NULL) AS tables, "
         "count(*) FILTER (WHERE kind = 'column' "
         "AND json_extract_string(attrs, '$.profile') IS NOT NULL) AS columns FROM nodes"),
        ("meta", "SELECT key, left(value, 80) AS val FROM meta ORDER BY key"),
    ]
    for title, sql in checks:
        print("\n== " + title)
        con.sql(sql).show(max_rows=25)
    con.close()
    wiki_dir = os.path.join(_work(args), "wiki")
    if os.path.isdir(wiki_dir):
        broken = wiki.check_links(wiki_dir)
        print("\n== broken wikilinks: " + str(len(broken)))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="src/run.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", default=None,
                        help="the lakehouse this context lives in, <workspace>/<lakehouse>. "
                             "Needed once; context.json remembers it after that")
    parser.add_argument("--work", default=None,
                        help="working folder for raw/, build/, wiki/ and graph.html. "
                             "Default: a folder per context under " + common.CACHE_DIR)
    parser.add_argument("--folder", default="context",
                        help="workspace folder the lakehouse and the notebook live in; "
                             "empty string for the workspace root")
    parser.add_argument("--no-push", action="store_true",
                        help="leave what this step produced in the working folder")
    parser.add_argument("--context", default=None,
                        help="read the published context from here instead of the address "
                             "in context.json: <ws-guid>/<lh-guid>, an abfss:// URL, or a "
                             "local folder of Delta tables")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="pull Fabric into the working raw/, then Files/raw")
    h.add_argument("--workspace", action="append", default=[],
                   help="workspace name or GUID; repeat for more")
    h.add_argument("--days", type=int, default=28, help="days of activity events (max 30)")
    h.add_argument("--refresh", action="store_true", help="refetch everything")
    h.add_argument("--no-scanner", action="store_true")
    h.add_argument("--no-activity", action="store_true")
    h.add_argument("--query-log", action="store_true",
                   help="also read each workspace's monitoring Eventhouse for the DAX that "
                        "actually ran. Needs workspace monitoring enabled there, which "
                        "bills against the capacity; workspaces without it are skipped")
    h.set_defaults(func=_harvest)

    b = sub.add_parser("build", help="raw/ -> the graph -> the lakehouse")
    b.set_defaults(func=lambda args: _build(args).close())

    w = sub.add_parser("wiki", help="the published context -> wiki/, then Files/wiki")
    w.set_defaults(func=_wiki)

    v = sub.add_parser("viz", help="the published context -> graph.html, then Files/")
    v.add_argument("--copy-to", default=None,
                   help="also write the html here, relative to the repo - e.g. "
                        "docs/index.html to publish it as a GitHub Page")
    v.set_defaults(func=_viz)

    p = sub.add_parser("profile", help="lakehouse columns, stats and values -> raw/*/profiles/")
    p.add_argument("--store", action="append", default=[],
                   help="only this lakehouse (name or GUID); repeat for more")
    p.add_argument("--all", action="store_true",
                   help="every table, not only the ones something reads or writes")
    p.add_argument("--no-values", action="store_true", help="skip the distinct-value scan")
    p.add_argument("--dry-run", action="store_true", help="list what would be profiled")
    p.set_defaults(func=_profile)

    f = sub.add_parser("files", help="the working files <-> the lakehouse Files/ section")
    f.add_argument("action", choices=["push", "pull", "list"])
    f.add_argument("--item", action="append", default=[],
                   help="raw, build, wiki or graph.html; repeat for more (default: all)")
    f.add_argument("--full", action="store_true",
                   help="push every file, not only what changed since the last push")
    f.set_defaults(func=_files)

    a = sub.add_parser("all", help="harvest, build, profile, build, wiki, viz")
    a.add_argument("--workspace", action="append", default=[])
    a.add_argument("--days", type=int, default=28)
    a.add_argument("--refresh", action="store_true")
    a.add_argument("--no-scanner", action="store_true")
    a.add_argument("--no-activity", action="store_true")
    a.add_argument("--query-log", action="store_true")
    a.add_argument("--no-profile", action="store_true")
    a.add_argument("--store", action="append", default=[])
    a.add_argument("--all", action="store_true")
    a.add_argument("--no-values", action="store_true")
    a.add_argument("--dry-run", action="store_true")

    def _all(args):
        # One process, one in-memory context: the chained steps never pull it back down.
        _harvest(args)
        con = _build(args)
        if not args.no_profile:
            _profile(args, con)
            con.close()
            con = _build(args)          # publish again, now with the profiles attached
        try:
            _wiki(args, con)
            _viz(args, con)
        finally:
            con.close()
    a.set_defaults(func=_all)

    q = sub.add_parser("query", help="run SQL against the published context")
    q.add_argument("sql")
    q.add_argument("--limit", type=int, default=40)
    q.set_defaults(func=_query)

    l = sub.add_parser("lineage", help="walk upstream (default) or downstream of a node")
    l.add_argument("node")
    l.add_argument("--down", action="store_true")
    l.add_argument("--depth", type=int, default=12)
    l.set_defaults(func=_lineage)

    d = sub.add_parser("deploy", help="ship the code and schedule the nightly refresh")
    d.add_argument("--name", default="context_refresh", help="the notebook's display name")
    d.add_argument("--workspace", action="append", default=[],
                   help="workspace to harvest; repeat. Default: the ones already in the context")
    d.add_argument("--daily", default="03:00", help="local time to run, HH:MM")
    d.add_argument("--tz", default="UTC", help='e.g. "AUS Eastern Standard Time"')
    d.add_argument("--no-schedule", action="store_true", help="deploy without scheduling")
    d.add_argument("--days", type=int, default=28, help="days of activity events per run")
    d.add_argument("--stale-after-days", type=float, default=1.0,
                   help="refetch the scanner and store table lists once older than this")
    d.add_argument("--query-log", action="store_true",
                   help="read the monitoring Eventhouse nightly too (see harvest --query-log)")
    d.add_argument("--no-profile", action="store_true", help="skip profiling in the nightly run")
    d.add_argument("--no-wiki", action="store_true", help="skip wiki and graph.html nightly")
    d.add_argument("--run", action="store_true", help="trigger a run now and wait for it")
    d.set_defaults(func=_deploy)

    c = sub.add_parser("check", help="verification counts")
    c.set_defaults(func=_check)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
