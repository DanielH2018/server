#!/bin/bash
# Container entrypoint of the nut pod: stages the NUT config, then starts the UPS driver,
# upsd and upsmon.
#
# Usage: /entrypoint.sh (no arguments)
# Runs as the image ENTRYPOINT (templates/Dockerfile.j2) in the nut pod on daniel-server. It
# reads the config files the nut-config Secret mounts at /nut-config and no environment
# variables. It ends in `exec upsmon -D`, so upsmon becomes the container's main process.
set -e

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

# Config arrives as a Secret volume (symlinked, root-owned). NUT's tools want real files
# owned root:nut and not world-readable, so stage a copy rather than mounting onto
# /etc/nut directly.
mkdir -p /etc/nut
cp -L /nut-config/* /etc/nut/
chown -R root:nut /etc/nut
chmod 640 /etc/nut/*

mkdir -p /var/run/nut
chown nut:nut /var/run/nut

# Start UPS driver (communicates with UPS hardware)
upsdrvctl start

# Start upsd (NUT server, allows clients to query UPS status)
upsd

# Start upsmon in foreground (monitors UPS, raises FSD on low battery; the host-side
# secondary upsmon performs the actual poweroff — see role CLAUDE.md, two-tier shutdown)
exec upsmon -D
