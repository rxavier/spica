"""Named queries, one module per table family.

Modules in here own their table's SQL; callers pass a connection and get rows or
plain values back. Connection/schema management lives in the store modules and
`spica.db`.
"""
