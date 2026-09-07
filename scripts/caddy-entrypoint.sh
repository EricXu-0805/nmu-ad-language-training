#!/bin/sh
# Embedded edge startup only. Pins match deploy/caddy-build.json; the independent
# build receipt, artifact hash and scan remain mandatory publication evidence.
# `caddy build-info` prints Go debug.BuildInfo: one `go<TAB>go1.x.y` line.
set -eu

reject() {
  printf '%s\n' 'REJECTED code=edge_binary_build_not_approved' >&2
  exit 78
}

[ "$#" -eq 0 ] || reject
edge_version=$(caddy version 2>/dev/null) || reject
case "$edge_version" in
  'v2.11.4'|'v2.11.4 '*) ;;
  *) reject ;;
esac
edge_build_info=$(caddy build-info 2>/dev/null) || reject
printf '%s\n' "$edge_build_info" | awk '
  $1 == "go" {
    count++
    if (NF != 2 || $2 != "go1.26.8") invalid = 1
  }
  END { exit (count != 1 || invalid) }
' || reject

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
