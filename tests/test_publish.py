"""Publishing, reading back, and moving files - against a local folder, so the real Delta
writer and the real push run with no tenant in sight.

The column contract below is copied from the query side (`ask/context.py`). It is the actual
interface between the two halves: an agent asks for a definition and gets a rank, a score and
a dataset id to execute against. Breaking it is the one change here that silently breaks
something else, so it is asserted rather than assumed.
"""
import os

import pytest

REQUIRED = {
    "nodes": ["id", "kind", "name", "workspace", "item_id", "parent_id", "description",
              "endorsement", "owner", "modified_at", "attrs"],
    "edges": ["src", "dst", "rel", "weight", "attrs"],
    "terms": ["term_id", "label", "n_definitions", "n_distinct_expr", "n_items",
              "conflicting", "top_def_id", "views", "n_reports"],
    "definitions": ["def_id", "term_id", "name", "kind", "workspace", "owner_item_id",
                    "owner_item_name", "table_name", "expression", "description",
                    "endorsement", "modified_at", "n_reports", "n_visuals", "views",
                    "authority", "popularity", "relevance", "freshness", "score", "rank",
                    "conflicting"],
    "flow": ["up", "down", "rel"],
    "measure_usage": ["def_id", "report_id", "n_visuals"],
    "item_views": ["item_id", "views"],
}
OPTIONAL = {
    "aliases": ["term_id", "alias", "alias_norm", "kind", "source_id"],
    "item_usage": ["item_id", "views", "runs", "queries", "refreshes", "users", "last_seen"],
    "query_usage": ["def_id", "item_id", "queries", "report_queries", "adhoc_queries",
                    "users", "last_queried"],
    "query_stats": ["item_id", "workspace", "queries", "measureless_queries", "window_days"],
    "meta": ["key", "value"],
}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from fabcontext._fabric import LocalStore

    return LocalStore(str(tmp_path_factory.mktemp("lakehouse")))


def test_publish_writes_delta(con, store):
    from fabcontext import publish

    counts = publish.publish(con, store)
    assert counts["nodes"] > 30, counts
    assert os.path.isdir(os.path.join(store.tables_root, "dbo", "nodes", "_delta_log"))


def test_publishing_again_replaces_rather_than_appends(con, store):
    """Create-or-update, with no tenant: a second run over the same store must leave the same
    number of rows, not twice as many."""
    from fabcontext import publish

    first = publish.publish(con, store)
    second = publish.publish(con, store)
    assert first == second
    back = _read(store)
    try:
        rows = back.execute("SELECT count(*) FROM nodes").fetchone()[0]
        assert rows == first["nodes"]
    finally:
        back.close()


def _read(store):
    from fabcontext import graph

    return graph.read_published(store)


def test_the_published_contract_holds(con, store):
    from fabcontext import publish

    publish.publish(con, store)
    back = _read(store)
    try:
        present = {r[0] for r in back.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        for table, columns in REQUIRED.items():
            assert table in present, table
            have = {c[0] for c in back.execute(
                "SELECT * FROM " + table + " LIMIT 0").description}
            assert set(columns) <= have, (table, sorted(set(columns) - have))
        for table, columns in OPTIONAL.items():
            if table not in present:
                continue
            have = {c[0] for c in back.execute(
                "SELECT * FROM " + table + " LIMIT 0").description}
            assert set(columns) <= have, (table, sorted(set(columns) - have))
    finally:
        back.close()


def test_the_ranking_survives_the_round_trip(con, store):
    from fabcontext import publish

    publish.publish(con, store)
    back = _read(store)
    try:
        rows = back.execute("SELECT rank, name FROM definitions WHERE term_id = 'revenue' "
                            "ORDER BY rank").fetchall()
        assert len(rows) == 3 and rows[0][1] == "Total Revenue", rows
        assert back.execute("SELECT count(*) FROM flow").fetchone()[0] > 0
    finally:
        back.close()


# ----------------------------------------------------------------------------- files

def test_files_push_is_a_diff(tmp_path):
    from fabcontext import files
    from fabcontext._fabric import LocalStore

    store = LocalStore(str(tmp_path / "lakehouse"))
    work = tmp_path / "work"
    (work / "raw").mkdir(parents=True)
    (work / "raw" / "a.json").write_text("{}", encoding="utf-8")
    (work / "raw" / "nested").mkdir()
    (work / "raw" / "nested" / "b.json").write_text("{}", encoding="utf-8")

    first = files.push(store, str(work), ["raw"], log=lambda _m: None)
    assert first["raw"]["sent"] == 2
    assert sorted(store.list("raw")) == ["a.json", "nested/b.json"]

    again = files.push(store, str(work), ["raw"], log=lambda _m: None)
    assert again["raw"] == {"sent": 0, "deleted": 0, "skipped": 2, "bytes": 0}

    os.remove(work / "raw" / "a.json")
    after = files.push(store, str(work), ["raw"], log=lambda _m: None)
    assert after["raw"]["deleted"] == 1
    assert store.list("raw") == ["nested/b.json"]


def test_files_pull_restores_the_tree(tmp_path):
    """The pull is what makes a second harvest incremental, so it has to bring back the tree
    intact - and leave the manifest saying so, or the next push re-sends everything."""
    from fabcontext import files
    from fabcontext._fabric import LocalStore

    store = LocalStore(str(tmp_path / "lakehouse"))
    work = tmp_path / "work"
    (work / "raw" / "nested").mkdir(parents=True)
    (work / "raw" / "a.json").write_text("{}", encoding="utf-8")
    (work / "raw" / "nested" / "b.json").write_text("{}", encoding="utf-8")
    files.push(store, str(work), ["raw"], log=lambda _m: None)

    back = tmp_path / "elsewhere"
    files.pull(store, str(back), ["raw"], log=lambda _m: None)
    assert (back / "raw" / "a.json").exists()
    assert (back / "raw" / "nested" / "b.json").exists()

    quiet = files.push(store, str(back), ["raw"], log=lambda _m: None)
    assert quiet["raw"]["sent"] == 0 and quiet["raw"]["skipped"] == 2


def test_the_whole_second_half_runs(tmp_path):
    """`build_and_publish` is what `harvest` calls once the tenant has been read, and it is
    the half that can run with no tenant at all - so it is exercised whole, not in pieces."""
    from fabcontext import build_and_publish
    from fabcontext._fabric import LocalStore
    from tests import fixtures

    work = tmp_path / "work"
    (work / "raw").mkdir(parents=True)
    fixtures.make_raw(str(work / "raw"))
    store = LocalStore(str(tmp_path / "lakehouse"))

    out = build_and_publish(str(work), store, profile=False, wiki=True,
                            log=lambda *_a, **_k: None)
    assert [status for _n, _t, status in out["steps"]] == ["ok"] * 5
    assert len(out["tables"]) == 13 and out["tables"]["nodes"] > 30
    assert len(store.list("raw")) > 20            # the next run's incremental starting point
    assert len(store.list("wiki")) > 10
    assert os.path.isfile(os.path.join(store.root, "graph.html"))
