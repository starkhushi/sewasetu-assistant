#!/bin/sh
# One-time preparation of a fresh Ubuntu 22.04/24.04 VM for Sewa Setu, then start it.
#
#   sudo sh deploy/setup.sh sewasetu.example.in 'a-strong-officer-password'
#
# Run from the repo folder on the VM. Safe to run again (it skips what is done).
set -e

DOMAIN="$1"
ADMIN_PASSWORD="$2"
if [ -z "$DOMAIN" ] || [ -z "$ADMIN_PASSWORD" ]; then
    echo "usage: sudo sh deploy/setup.sh <domain> <officer-password>"; exit 1
fi

# Docker and the compose plugin
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi

# Small VMs: building images and loading 100k seed rows wants some swap
if ! swapon --show | grep -q swapfile; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Only SSH and the web are open; Postgres and the internal ports stay on localhost
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
fi

cat > .env <<EOF
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
DOMAIN=$DOMAIN
PUBLIC_BASE_URL=https://$DOMAIN
MCP_CITIZEN_URL=https://$DOMAIN/mcp/citizen
MCP_OFFICER_URL=https://$DOMAIN/mcp/officer
EOF
chmod 600 .env

# every service has restart: unless-stopped in the prod overlay, so it survives reboots
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d

echo
echo "Starting. The database loads the seed on first boot (a minute or two)."
echo "Then check:  https://$DOMAIN   https://$DOMAIN/agents   https://$DOMAIN/eval-dashboard"
