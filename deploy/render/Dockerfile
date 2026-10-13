# Everything in one container, for free hosts that give one web service and no
# card-free VM (Render's free plan). Caddy listens on $PORT and routes the two MCP
# endpoints and the portal; the SMS gateway and the nightly job run alongside.
# The database is external (DATABASE_URL), e.g. Neon. Build from the repo root:
#   docker build -f deploy/render/Dockerfile .
FROM caddy:2 AS caddy

FROM python:3.11-slim
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

# Two virtualenvs: the portal pins PyJWT 2.9 and the MCP SDK needs a newer one.
COPY app/requirements.txt /tmp/app-requirements.txt
COPY mcp/requirements.txt /tmp/mcp-requirements.txt
RUN python -m venv /venv/app && /venv/app/bin/pip install --no-cache-dir -r /tmp/app-requirements.txt \
 && python -m venv /venv/mcp && /venv/mcp/bin/pip install --no-cache-dir -r /tmp/mcp-requirements.txt

COPY app /app
COPY smsgw /smsgw
COPY mcp/sewasetu_mcp /srv/sewasetu_mcp
COPY evals/results /app/eval_results
COPY deploy/render/Caddyfile /etc/caddy/Caddyfile
COPY deploy/render/start.sh /start.sh
RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu

ENV PORT=10000
EXPOSE 10000
CMD ["bash", "/start.sh"]
