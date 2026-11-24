"""Named queries, one module per store family.

Modules in here own their store's SQL; callers pass a connection and get rows or
plain values back. Connection/schema management lives in the store modules and
`spica.db`.
"""
