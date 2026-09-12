import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures  # noqa: E402


@pytest.fixture(scope="session")
def raw(tmp_path_factory):
    """A harvested synthetic tenant on disk."""
    folder = tmp_path_factory.mktemp("tenant") / "raw"
    fixtures.make_raw(str(folder))
    return str(folder)


@pytest.fixture(scope="session")
def built(raw, tmp_path_factory):
    """(connection, counts) over the ranked graph. Session-scoped: the build is the same for
    every assertion, and doing it once keeps the suite quick."""
    from fabcontext import graph, parse

    build_dir = str(tmp_path_factory.mktemp("build"))
    parse.build(raw, build_dir)
    con, counts = graph.build(build_dir)
    yield con, counts
    con.close()


@pytest.fixture(scope="session")
def con(built):
    return built[0]


@pytest.fixture(scope="session")
def wiki_dir(con, tmp_path_factory):
    from fabcontext import wiki

    out = str(tmp_path_factory.mktemp("wiki"))
    wiki.render(con, out)
    return out
