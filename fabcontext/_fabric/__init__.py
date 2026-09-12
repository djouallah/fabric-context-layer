"""Everything that talks to Fabric, and nothing that understands the tenant.

The split is deliberate: above this package the code is about semantic models, terms and
ranking; inside it, about tokens, retries, Delta files and REST pagination. The harvest used
to get all of this from a third-party library that pins duckdb and deltalake to versions the
Fabric Python runtime does not ship - which forced a pip upgrade of two native libraries and
a kernel restart before anything could import them.

So it is vendored here instead, sized to exactly what the harvest does, against the versions
Fabric already has. Nothing in this package imports that library, and nothing should.
"""
from __future__ import annotations

from . import auth, delta, onelake, patterns, rest
from .onelake import LocalStore, OneLakeStore
from .rest import FabricError
from .workspace import Workspace

__all__ = ["auth", "delta", "onelake", "patterns", "rest",
           "LocalStore", "OneLakeStore", "FabricError", "Workspace"]
