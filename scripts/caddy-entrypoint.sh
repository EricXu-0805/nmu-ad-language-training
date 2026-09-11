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
  BEGIN {
    expected["golang.org/x/crypto"] = "v0.55.0"
    expected["golang.org/x/net"] = "v0.58.0"
    expected["golang.org/x/text"] = "v0.41.0"
    expected["google.golang.org/grpc"] = "v1.83.2"
  }
  $1 == "go" {
    count++
    if (NF != 2 || $2 != "go1.26.8") invalid = 1
  }
  $1 == "dep" && ($2 in expected) {
    dependencies[$2]++
    if (NF != 4 || $3 != expected[$2] || $4 !~ /^h1:[A-Za-z0-9+\/=]+$/) invalid = 1
  }
  # A replacement can retain the approved label while running different code.
  $1 == "=>" { invalid = 1 }
  END {
    for (module in expected) if (dependencies[module] != 1) invalid = 1
    exit (count != 1 || invalid)
  }
' || reject

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
