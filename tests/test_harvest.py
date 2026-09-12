"""The harvest end to end on a synthetic tenant: parse, rank, profile, publish, render.

No network anywhere. A local folder is a perfectly good Delta store, so the publish, the
read-back and the file push are the real code paths, not stand-ins - which is the only way
this suite says anything about what runs in a notebook.
"""
import os

import pytest

from tests.fixtures import (DEFAULT_MODEL, LH, LH2, MODEL_A, MODEL_B, NOTEBOOK, PIPELINE,
                            REPORT_A, WS, WS2)


# --------------------------------------------------------------------------- parse

def test_parse_emits_a_graph(built):
    _con, counts = built
    assert counts["nodes"] > 30
    assert counts["edges"] > 30


def test_no_dangling_edges(con):
    """Every edge lands on a node, or on a deliberately nodeless `unresolved:` placeholder.
    Anything else is a parser that invented an id."""
    dangling = con.execute(
        "SELECT count(*) FROM edges e LEFT JOIN nodes n ON n.id = e.dst "
        "WHERE n.id IS NULL AND e.dst NOT LIKE 'unresolved:%'").fetchone()[0]
    assert dangling == 0


# --------------------------------------------------------------------- the ranking

def test_conflicting_definitions_are_ranked(con):
    rows = con.execute(
        "SELECT rank, name, owner_item_name, endorsement, views, n_reports, conflicting "
        "FROM definitions WHERE term_id = 'revenue' ORDER BY rank").fetchall()
    assert len(rows) == 3, rows
    assert rows[0][1] == "Total Revenue", rows          # the certified model wins
    assert rows[0][6] is True                           # and the disagreement is flagged


def test_a_qualified_term_stays_its_own(con):
    """Revenue YTD is not Revenue. The keep-list is what stops the normaliser merging them."""
    assert con.execute(
        "SELECT count(*) FROM terms WHERE term_id = 'revenue-ytd'").fetchone()[0] == 1


def test_spellings_merge_into_one_term(con):
    rows = con.execute("SELECT rank, name, conflicting FROM definitions "
                       "WHERE term_id = 'avg-price' ORDER BY rank").fetchall()
    assert len(rows) == 2 and rows[0][2] is True, rows
    assert rows[0][1] == "Price_AVG", rows
    aliases = con.execute("SELECT list(alias ORDER BY alias) FROM aliases "
                          "WHERE term_id = 'avg-price'").fetchone()[0]
    assert aliases == ["Average Price", "Price_AVG"]
    label = con.execute("SELECT label FROM terms WHERE term_id = 'avg-price'").fetchone()[0]
    assert label == "Price_AVG"                         # the winner names the term


# ------------------------------------------------------------------------- bindings

def test_dax_reference_between_measures(con):
    found = con.execute(
        "SELECT count(*) FROM edges WHERE rel = 'references' "
        "AND json_extract_string(attrs, '$.via') = 'dax' AND src LIKE 'measure:%'").fetchone()[0]
    assert found >= 1


def test_partitions_bind_to_physical_tables(con):
    bound = [r[0] for r in con.execute(
        "SELECT dst FROM edges WHERE rel = 'sources_from' AND src LIKE 'model_table:%'"
    ).fetchall() if r[0].startswith("lakehouse_table:")]
    assert any("fact_sales" in b for b in bound), bound  # Direct Lake
    assert len(bound) >= 3, bound                        # and DirectQuery through M


def test_visual_field_references_resolve_in_both_report_formats(con):
    found = con.execute("SELECT count(*) FROM edges e JOIN nodes n ON n.id = e.src "
                        "WHERE n.kind = 'visual' AND e.rel = 'references'").fetchone()[0]
    assert found >= 3


def test_code_reads_and_writes(con):
    feeds = [r[0] for r in con.execute(
        "SELECT dst FROM edges WHERE rel = 'feeds' AND src = ?",
        ["notebook:" + NOTEBOOK]).fetchall()]
    reads = [r[0] for r in con.execute(
        "SELECT dst FROM edges WHERE rel = 'reads' AND src = ?",
        ["notebook:" + NOTEBOOK]).fetchall()]
    assert any("fact_sales" in f for f in feeds), feeds
    assert any("dim_customer" in r for r in reads), reads
    assert any("dim_date" in r for r in reads), reads    # SQL inside a string literal
    runs = con.execute("SELECT count(*) FROM edges WHERE rel IN ('runs','refreshes') "
                       "AND src = ?", ["pipeline:" + PIPELINE]).fetchone()[0]
    assert runs == 2


def test_python_imports_are_not_mistaken_for_tables(con):
    noise = con.execute(
        "SELECT count(*) FROM edges WHERE dst LIKE 'unresolved:table/deltalake%' "
        "OR dst LIKE 'unresolved:table/psutil%' "
        "OR dst LIKE 'unresolved:table/shutil%'").fetchone()[0]
    assert noise == 0


def test_a_store_outside_the_harvest_becomes_a_stub(con):
    """An unharvested store gets a node, so the edge resolves and a reader sees 'external'
    rather than nothing at all."""
    external = [r[0] for r in con.execute(
        "SELECT id FROM nodes WHERE kind = 'lakehouse_table' "
        "AND TRY_CAST(attrs->>'external' AS BOOLEAN)").fetchall()]
    assert any(LH2 in e for e in external), external
    workspace = con.execute("SELECT attrs->>'workspace_id' FROM nodes WHERE id = ?",
                            ["lakehouse:" + LH2]).fetchone()
    assert workspace and workspace[0] == WS2


# -------------------------------------------------------------------------- lineage

def test_lineage_walks_to_the_physical_table(con):
    from fabcontext import graph

    top = con.execute("SELECT top_def_id FROM terms WHERE term_id = 'revenue'").fetchone()[0]
    kinds = {kind for _id, kind, _n, _w, _d, _r in graph.lineage(con, top, "up", 8)}
    assert {"lakehouse_table", "notebook", "pipeline"} <= kinds, sorted(kinds)


def test_lineage_reaches_an_external_table(con):
    from fabcontext import graph

    rows = graph.lineage(con, "model_table:" + MODEL_B + "/External", "up", 4)
    assert any(row[1] == "lakehouse_table" for row in rows), rows


# ---------------------------------------------------------------------------- usage

def test_item_usage_buckets_activity_and_drops_harvest_noise(con):
    assert con.execute("SELECT views, runs, events FROM item_usage WHERE item_id = ?",
                       [MODEL_A]).fetchone() == (1, 1, 2)
    views = con.execute("SELECT views FROM item_views WHERE item_id = ?", [REPORT_A]).fetchone()
    assert views and views[0] == 36


def test_query_log_is_attributed_to_the_measures_the_dax_called(con):
    counts = dict(con.execute(
        "SELECT split_part(def_id, '/', -1), queries FROM query_usage").fetchall())
    assert counts.get("Price_AVG") == 120 and counts.get("Total Revenue") == 15, counts
    split = con.execute("SELECT report_queries, adhoc_queries FROM query_usage "
                        "WHERE def_id LIKE '%Price_AVG'").fetchone()
    assert split == (0, 120)


def test_queries_that_re_derive_a_measure_inline_are_counted(con):
    inline = con.execute("SELECT measureless_queries FROM query_stats WHERE item_id = ?",
                         [MODEL_A]).fetchone()[0]
    assert inline == 21
    watched = con.execute("SELECT count(*) FROM query_stats WHERE queries = 0").fetchone()[0]
    assert watched == 1          # monitored but unqueried is not the same as unmonitored


def test_query_counts_lift_a_measure_no_report_references(con):
    """The point of reading the query log: a measure nobody wrote into a report still ranks
    first when people actually ask for it."""
    row = con.execute("SELECT name, queries, views FROM definitions "
                      "WHERE term_id = 'avg-price' AND rank = 1").fetchone()
    assert row == ("Price_AVG", 120, 0)


# ------------------------------------------------------------------------- profiles

def test_profiles_reach_the_model_columns(con):
    profile = con.execute("SELECT attrs->'profile' FROM nodes WHERE id = ?",
                          ["column:" + MODEL_A + "/Sales/Region"]).fetchone()[0]
    assert profile and "NSW" in str(profile)
    rows = con.execute("SELECT attrs->>'n_rows' FROM nodes WHERE id = ?",
                       ["lakehouse_table:" + LH + "/dbo/fact_sales"]).fetchone()[0]
    assert rows == "1200"
    columns = con.execute("SELECT count(*) FROM nodes WHERE kind = 'column' AND parent_id = ?",
                          ["lakehouse_table:" + LH + "/dbo/fact_sales"]).fetchone()[0]
    assert columns == 3


# ----------------------------------------------------------------------------- tier

def test_the_long_tail_is_demoted(con):
    tiers = dict(con.execute(
        "SELECT name, tier FROM nodes WHERE kind = 'lakehouse_table'").fetchall())
    assert tiers.get("fact_sales") == 1          # a model sources from it
    assert tiers.get("scratch_tmp") == 3         # nothing refers to it
    endpoints = con.execute("SELECT count(*) FROM nodes WHERE kind = 'sql_endpoint' "
                            "AND tier < 3").fetchone()[0]
    assert endpoints == 0                        # plumbing is never load-bearing


def test_the_default_semantic_model_is_deleted_not_demoted(con):
    """Fabric makes one beside every lakehouse, with synced tables and no measure. It
    defines nothing, so it has no tier - it is not in the graph at all."""
    assert con.execute("SELECT count(*) FROM nodes WHERE item_id = ?",
                       [DEFAULT_MODEL]).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM edges WHERE src LIKE ? OR dst LIKE ?",
                       ["%" + DEFAULT_MODEL + "%"] * 2).fetchone()[0] == 0
    # the models a person built keep their tier
    assert dict(con.execute("SELECT name, tier FROM nodes WHERE kind = 'semantic_model'"
                            ).fetchall()) == {"Sales Model": 1, "Finance Model": 1}


def test_the_default_model_does_not_promote_the_tail(con):
    """Its tables `sources_from` every lakehouse table, the edge that means tier 1. Left in
    the graph it would make a sandbox table look load-bearing and the tier mean nothing."""
    assert con.execute("SELECT tier FROM nodes WHERE kind = 'lakehouse_table' "
                       "AND name = 'scratch_tmp'").fetchone()[0] == 3
    # the delete leaves nothing behind it: no edge, and no monitoring row either
    assert con.execute("SELECT count(*) FROM edges e WHERE NOT EXISTS "
                       "(SELECT 1 FROM nodes n WHERE n.id = e.src)").fetchone()[0] == 0
    for table in ("query_stats", "query_usage", "activity"):
        assert con.execute("SELECT count(*) FROM " + table + " WHERE item_id = ?",
                           [DEFAULT_MODEL]).fetchone()[0] == 0, table


def test_meta_records_provenance(con):
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    assert meta.get("built_at")
    assert "tiers" in meta
    assert meta.get("schema_version")


# ----------------------------------------------------------------------------- wiki

def test_wiki_pages(con, wiki_dir):
    from fabcontext import wiki

    n_terms = con.execute("SELECT count(*) FROM terms").fetchone()[0]
    pages = wiki.render(con, wiki_dir)
    assert pages.get("term", 0) == n_terms
    assert (pages.get("semantic_model") == 2 and pages.get("report") == 2
            and pages.get("lakehouse_table") == 4 and pages.get("notebook") == 1), pages
    assert pages.get("tail", 0) >= 1
    assert not os.path.exists(os.path.join(wiki_dir, "tables",
                                           "sales_lh--22222222.dbo.scratch_tmp.md"))
    assert not wiki.check_links(wiki_dir)
    assert os.path.exists(os.path.join(wiki_dir, "CLAUDE.md"))


def test_the_term_page_shows_the_conflict(wiki_dir):
    text = open(os.path.join(wiki_dir, "terms", "revenue.md"), encoding="utf-8").read()
    assert "disagree" in text
    assert "```dax" in text
    assert "Sales Overview" in text


def test_a_demoted_table_still_appears_on_its_store_page(wiki_dir):
    page = open(os.path.join(wiki_dir, "stores", "sales_lh--22222222.md"),
                encoding="utf-8").read()
    assert "`scratch_tmp`" in page and "Not referenced" in page


def test_the_table_page_lists_profiled_columns(wiki_dir):
    page = open(os.path.join(wiki_dir, "tables", "sales_lh--22222222.dbo.fact_sales.md"),
                encoding="utf-8").read()
    assert "| Region | string | 2 |" in page


# ----------------------------------------------------------------------------- context.md

def _section(text, heading):
    """One `### kind: name` block, up to the next one."""
    assert heading in text, heading
    rest = text.split(heading, 1)[1]
    return heading + rest.split("\n### ", 1)[0]


def test_context_md_is_one_file(con, rendered, context_md):
    from fabcontext import wiki

    out, md = rendered
    counts = wiki.render(con, out, md)
    assert counts["context_md"] == len(context_md.encode("utf-8")) > 2000
    assert "\r" not in context_md            # written \n-only, whatever the platform


def test_context_md_header_says_when_and_where(context_md):
    assert "built_at: " in context_md
    assert "schema_version: 4" in context_md
    assert "- workspace Sales Demo (" + WS + ")" in context_md
    assert "## How to use this file" in context_md
    assert "Rank 1 is the answer" in context_md


def test_context_md_ranks_the_competing_definitions(context_md):
    section = _section(context_md, "### term: revenue\n")
    assert "conflicting: yes" in section
    assert "| 1 | Total Revenue | Sales Model |" in section
    assert section.index("Total Revenue") < section.index("Finance Model")
    assert section.count("```dax") == 3       # every definition's expression, not just rank 1
    assert "SUM ( Sales[Amount] )" in section


def test_context_md_carries_what_a_dax_call_needs(context_md):
    section = _section(context_md, "### model: Sales Model\n")
    assert MODEL_A in section and WS in section
    assert "storage: DirectLake" in section
    assert "#### measure: [Total Revenue] on Sales" in section
    assert "values: NSW, VIC" in section       # filter literals, so nothing is invented
    # The client has no lookup: the ids `ask dax` takes are copied straight off the page.
    assert "run: python -m ask dax " + WS + "/" + MODEL_A in section


def test_context_md_offers_no_route_but_dax(context_md):
    """The file is the metadata; DAX is the only number. Nothing may suggest SQL."""
    assert "ask sql" not in context_md and "select ... from" not in context_md
    assert "**When no measure covers it, say so.**" in context_md
    assert "python -m ask dax" in context_md


def test_context_md_names_the_tail_without_detailing_it(context_md):
    assert context_md.count("scratch_tmp") == 1
    assert "not referenced (1)" in context_md
    assert "#### table: sales_lh.dbo.scratch_tmp" not in context_md
    assert "| Region | string | 2 |" in _section(context_md,
                                                 "### store: sales_lh (lakehouse)\n")


def test_context_md_shows_what_writes_what(context_md):
    notebook = _section(context_md, "### notebook: load_sales\n")
    assert "- writes: sales_lh.dbo.fact_sales" in notebook
    pipeline = _section(context_md, "### pipeline: nightly\n")
    assert "- refreshes: Sales Model (semantic model)" in pipeline
