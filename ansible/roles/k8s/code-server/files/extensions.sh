#!/bin/bash
# Install proprietary extensions (downloaded at image build time) into the
# user config volume on each container start. code-server handles idempotency.
#
# --extensions-dir is NOT optional, and its absence was a silent bug from this script's first
# commit (d9a33181) until 2026-08-16. The server is launched by the image's own run script with
# `--extensions-dir /config/extensions`; this CLI invocation inherits none of those arguments,
# so without the flag it installed into code-server's DEFAULT directory instead —
# /config/.local/share/code-server/extensions, which the running server never reads. That
# directory had reached 5.8 G by 2026-08-16 against 637 M in the live one: every image rebuild
# fetches the current version of each extension, and nothing ever removed the previous ones.
EXTENSIONS_DIR=/config/extensions

for vsix in /opt/vsix/*.vsix; do
    echo "Installing extension: ${vsix}"
    /app/code-server/bin/code-server --extensions-dir "${EXTENSIONS_DIR}" --install-extension "${vsix}"
done
