#!/bin/bash
# Start every part of Sewa Setu in one container. If any of them dies, exit, so the
# host restarts the whole container rather than leaving it half-working.

# Render tells the service its public address; everything else follows from it.
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-$RENDER_EXTERNAL_URL}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
export MCP_CITIZEN_URL="${MCP_CITIZEN_URL:-$PUBLIC_BASE_URL/mcp/citizen}"
export MCP_OFFICER_URL="${MCP_OFFICER_URL:-$PUBLIC_BASE_URL/mcp/officer}"
export SMS_GATEWAY_URL=http://127.0.0.1:8025
export PORTAL_INTERNAL_URL=http://127.0.0.1:8000
export EVAL_RESULTS_DIR=/app/eval_results

echo "Sewa Setu at $PUBLIC_BASE_URL"

(cd /smsgw && exec /venv/app/bin/python app.py) &
(cd /app && exec /venv/app/bin/gunicorn --bind 127.0.0.1:8000 --workers "${WEB_WORKERS:-2}" \
    --threads 4 --timeout 120 app:app) &
(cd /srv && PORT=8101 exec /venv/mcp/bin/python -m sewasetu_mcp.citizen) &
(cd /srv && PORT=8102 exec /venv/mcp/bin/python -m sewasetu_mcp.officer) &
# deemed approval: once at start, then daily
(sleep 30; while true; do /venv/app/bin/python /app/scripts/deemed_approval.py; sleep 86400; done) &
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

wait -n
echo "a process exited; stopping so the container is restarted"
exit 1
