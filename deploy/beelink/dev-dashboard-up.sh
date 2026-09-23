#!/usr/bin/env bash
# Dashboard access on the Beelink (dev page now, Phase 9 dashboard later).
#
# Runs two containers with host networking, no sudo needed (docker group):
#   desk-dev-caddy  proxy on 127.0.0.1:8088 -> 127.0.0.1:18010, the SSH reverse tunnel
#                   from the workstation (deploy/dev/run-dev-dashboard.ps1)
#   desk-tunnel     Cloudflare named tunnel: desk.hoistlaboratory.com -> localhost:8088
#
# Authentication is Cloudflare Access (email one-time PIN, "Jon only" policy) in front of
# the hostname. Caddy listens on loopback only, so the tunnel is the only way in.
#
# Usage: printf 'TUNNEL_TOKEN=%s\n' "$TOKEN" | ./dev-dashboard-up.sh
# The token is read from stdin and stored in a 0600 env file, never on a command line.
set -euo pipefail

DIR="$HOME/desk-dev"
mkdir -p "$DIR"
umask 077
cat > "$DIR/tunnel.env"

cat > "$DIR/Caddyfile" <<'EOF'
:8088 {
	bind 127.0.0.1
	redir / /dev
	reverse_proxy 127.0.0.1:18010
}
EOF

docker rm -f desk-dev-caddy desk-dev-tunnel desk-tunnel >/dev/null 2>&1 || true
docker run -d --name desk-dev-caddy --network host --restart unless-stopped \
  -v "$DIR/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 >/dev/null
docker run -d --name desk-tunnel --network host --restart unless-stopped \
  --env-file "$DIR/tunnel.env" cloudflare/cloudflared:latest tunnel --no-autoupdate run >/dev/null

sleep 8
docker logs desk-tunnel 2>&1 | grep -c "Registered tunnel connection" | xargs echo "tunnel connections registered:"
