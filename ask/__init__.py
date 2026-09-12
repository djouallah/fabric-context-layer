"""The query side of the context layer.

Two commands and no database. The harvest side (`fabcontext`) publishes the ranked context
into a Fabric lakehouse and renders it as one markdown file; `python -m ask context` fetches
that file and `python -m ask dax` runs a number against a semantic model. The client imports
nothing from the harvest but its OneLake file access and its tokens.

There is no SQL path on purpose: a number comes from DAX calling a ranked measure by name,
or the honest answer is that nothing in the tenant defines one.
"""
