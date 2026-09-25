#!/usr/bin/env bash
# Dashboard on the Beelink: serves the built bundle and proxies API paths to the
# workstation through the SSH reverse tunnel (127.0.0.1:18010).
#
# Replaces desk-dev-caddy with desk-caddy (host network, loopback only). The Cloudflare
# tunnel container (desk-tunnel, from dev-dashboard-up.sh) is left as it is: it forwards
# desk.hoistlaboratory.com to 127.0.0.1:8088 behind Cloudflare Access.
#
# Usage: ./dashboard-up.sh   (publish.ps1 copies this script and the bundle first)
set -euo pipefail

DIR="$HOME/desk"
mkdir -p "$DIR/site"

cat > "$DIR/Caddyfile" <<'EOF'
:8088 {
	bind 127.0.0.1
	encode gzip

	# Everything the workstation API answers; the rest is the dashboard bundle.
	@api path /api/* /intake/* /positions/* /scores /scores.json /health /artifacts/* /briefs/* /alerts/* /dossiers/* /theses/* /plans/* /debates/* /dev /dev/*
	handle @api {
		reverse_proxy 127.0.0.1:18010 {
			flush_interval -1
		}
	}

	handle {
		root * /srv/site
		try_files {path} /index.html
		header /assets/* Cache-Control "public, max-age=31536000, immutable"
		header /index.html Cache-Control "no-cache"
		file_server
	}
}
EOF

docker rm -f desk-dev-caddy desk-caddy >/dev/null 2>&1 || true
docker run -d --name desk-caddy --network host --restart unless-stopped \
  -v "$DIR/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$DIR/site:/srv/site:ro" caddy:2 >/dev/null
sleep 2
docker ps --filter name=desk-caddy --format '{{.Names}} {{.Status}}'
