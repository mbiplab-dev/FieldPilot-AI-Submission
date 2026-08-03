"""Shared storage primitives used by both the backend service and the edge device.

`docstore` gives new domain tables (zones, feedback, learning runs, the offline outbox) one
implementation over SQLite *and* PostgreSQL, so adding a table costs a declaration instead of
two hand-written repository classes. The older explicit repositories in `backend.store` and
`events.store` predate it and are left as they are.
"""

from fieldpilot.storage.docstore import Column, DocStore, Table, TableSpec

__all__ = ["Column", "DocStore", "Table", "TableSpec"]
