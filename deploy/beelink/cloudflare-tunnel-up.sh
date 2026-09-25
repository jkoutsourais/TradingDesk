#!/usr/bin/env bash
# Cloudflare named tunnel on the Beelink: desk.hoistlaboratory.com -> 127.0.0.1:8088, where
# desk-caddy (dashboard-up.sh) serves the dashboard. Authentication is Cloudflare Access
# in front of the hostname; Caddy listens on loopback only, so the tunnel is the only way in.
#
# Usage: printf 'TUNNEL_TOKEN=%s
' "$TOKEN" | ./cloudflare-tunnel-up.sh
# The token is read from stdin and stored in a 0600 env file, never on a command line.
set -euo pipefail

DIR="$HOME/desk-dev"
mkdir -p "$DIR"
umask 077
cat > "$DIR/tunnel.env"

docker rm -f desk-tunnel >/dev/null 2>&1 || true
docker run -d --name desk-tunnel --network host --restart unless-stopped   --env-file "$DIR/tunnel.env" cloudflare/cloudflared:latest tunnel --no-autoupdate run >/dev/null

sleep 8
docker logs desk-tunnel 2>&1 | grep -c "Registered tunnel connection" | xargs echo "tunnel connections registered:"
