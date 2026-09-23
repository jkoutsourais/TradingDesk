#!/usr/bin/env bash
# Temporary remote dev dashboard (Beelink side). Removed with the Phase 9 dashboard.
#
# Runs two containers with host networking, no sudo needed (docker group):
#   desk-dev-caddy   basic-auth proxy on 127.0.0.1:8088 -> 127.0.0.1:18010, the SSH
#                    reverse tunnel from the workstation (deploy/dev/run-dev-dashboard.ps1)
#   desk-dev-tunnel  Cloudflare quick tunnel -> https://<random>.trycloudflare.com
#
# Usage: printf '%s\n%s\n' "$USER_NAME" "$PASSWORD" | ./dev-dashboard-up.sh
# The login is read from stdin so it never appears in a process list; only its bcrypt
# hash is written to disk.
set -euo pipefail

DIR="$HOME/desk-dev"
mkdir -p "$DIR"
read -r USER_NAME
read -r PASSWORD

HASH="$(printf '%s\n' "$PASSWORD" | docker run -i --rm caddy:2 caddy hash-password)"
cat > "$DIR/Caddyfile" <<EOF
:8088 {
	bind 127.0.0.1
	basic_auth {
		$USER_NAME $HASH
	}
	redir / /dev
	reverse_proxy 127.0.0.1:18010
}
EOF
chmod 600 "$DIR/Caddyfile"

docker rm -f desk-dev-caddy desk-dev-tunnel >/dev/null 2>&1 || true
docker run -d --name desk-dev-caddy --network host --restart unless-stopped \
  -v "$DIR/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 >/dev/null
docker run -d --name desk-dev-tunnel --network host --restart unless-stopped \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://127.0.0.1:8088 >/dev/null

for _ in $(seq 1 30); do
  URL="$(docker logs desk-dev-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [ -n "$URL" ] && break
  sleep 2
done
echo "${URL:-tunnel URL not found yet; check: docker logs desk-dev-tunnel}"
