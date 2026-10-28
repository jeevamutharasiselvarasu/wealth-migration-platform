"""Standalone simulated Simplicity target system (its own service and database).

This package is deliberately independent of the XODUS platform: it imports nothing from control_plane or ui,
owns its SQLite file, and exposes a read-only schema API. XODUS reaches it only through a configured
connection, as it would an external system. It holds no migration data and nothing is ever loaded into it.
"""
