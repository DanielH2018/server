#!/bin/bash
set -e

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
