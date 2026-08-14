#!/bin/bash
set -e

mkdir -p /var/run/nut
chown nut:nut /var/run/nut

# Start UPS driver (communicates with UPS hardware)
upsdrvctl start

# Start upsd (NUT server, allows clients to query UPS status)
upsd

# Start upsmon in foreground (monitors UPS, raises FSD on low battery; the host-side
# secondary upsmon performs the actual poweroff — see role CLAUDE.md, two-tier shutdown)
exec upsmon -D
