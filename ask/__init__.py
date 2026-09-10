"""The query side of the context layer.

Reads the Fabric lakehouse the harvest side (src/) publishes the context into - found
through context.json - and answers questions from it, with live calls to Fabric when a
question needs a number. It imports nothing from src/. The contract between the two sides
is the set of tables ask/context.py queries; python -m ask contract checks it.
"""
