#!/bin/sh
# One container for the demo: the simulated target (127.0.0.1:8100, its own SQLite DB) plus the platform.
python -m simulated_target_service &
i=0
until python -c "import urllib.request,sys; urllib.request.urlopen(\"http://127.0.0.1:${SIMULATED_TARGET_PORT:-8100}/health\", timeout=1)" 2>/dev/null; do
  i=$((i+1)); [ "$i" -ge 30 ] && echo "simulated target did not start" >&2 && break; sleep 1
done
exec uvicorn ui.app:app --host 0.0.0.0 --port "${PORT:-8000}"
