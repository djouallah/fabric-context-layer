"""`python -m fabcontext "<workspace>"` - the one call, from a command line.

The package does one thing, so this does one thing. It exists for a terminal session and for
CI; inside a notebook, call `harvest()` directly.
"""
from __future__ import annotations

import argparse
import sys

from . import __doc__ as _doc
from . import __version__, harvest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fabcontext", description=_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", nargs="+",
                        help="workspace name or GUID; repeat for more")
    parser.add_argument("--to", default=None,
                        help="the lakehouse to publish into, <workspace>/<lakehouse>. "
                             "Default: context_layer in the first workspace named")
    parser.add_argument("--days", type=int, default=28,
                        help="days of activity events to read (the audit log keeps 30)")
    parser.add_argument("--query-log", action="store_true",
                        help="also read each workspace's monitoring Eventhouse for the DAX "
                             "that actually ran. Needs monitoring enabled there, which bills "
                             "against the capacity; a workspace without it is skipped")
    parser.add_argument("--refresh", action="store_true",
                        help="refetch everything instead of only what changed")
    parser.add_argument("--stale-after-days", type=float, default=1.0,
                        help="refetch the scanner and the store table lists once older than "
                             "this; they carry no freshness signal of their own")
    parser.add_argument("--no-profile", action="store_true",
                        help="skip reading lakehouse columns, stats and values")
    parser.add_argument("--no-values", action="store_true",
                        help="profile from the Delta log only, without the distinct-value scan")
    parser.add_argument("--no-wiki", action="store_true",
                        help="skip the markdown wiki and graph.html")
    parser.add_argument("--folder", default="context",
                        help="workspace folder to put the lakehouse in; empty for the root")
    parser.add_argument("--work", default=None,
                        help="keep the working files here instead of a temporary folder")
    parser.add_argument("--version", action="version", version="fabcontext " + __version__)
    args = parser.parse_args(argv)

    # DuckDB prints tables with box-drawing characters and Fabric names are not all ASCII;
    # a stock Windows console is cp1252 and would raise on both.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    harvest(args.workspace, to=args.to, days=args.days, query_log=args.query_log,
            refresh=args.refresh, profile=not args.no_profile, values=not args.no_values,
            wiki=not args.no_wiki, folder=args.folder or None,
            stale_after_days=args.stale_after_days, work=args.work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
