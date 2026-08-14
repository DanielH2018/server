#!/bin/bash
# Install proprietary extensions (downloaded at image build time) into the
# user config volume on each container start. code-server handles idempotency.
for vsix in /opt/vsix/*.vsix; do
    echo "Installing extension: ${vsix}"
    /app/code-server/bin/code-server --install-extension "${vsix}"
done
