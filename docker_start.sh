#!/bin/sh
# The platform only. The target is the Azure SQL database saved on the Azure connection page; no target runs in this container.
RELOAD=""
[ -n "$XODUS_RELOAD" ] && RELOAD="--reload --reload-dir ui --reload-dir control_plane --reload-dir agents --reload-dir data_plane"
exec uvicorn ui.app:app --host 0.0.0.0 --port "${PORT:-8000}" $RELOAD
