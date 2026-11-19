#!/bin/bash
# Install proprietary extensions (downloaded at image build time) into the
# user config volume on each container start. code-server handles idempotency.
#
# Usage: extensions.sh (no arguments)
# Runs as /custom-cont-init.d/10-extensions.sh in the code-server image, on every start of the
# container. It uninstalls each id in RETIRED_EXTS, then installs every /opt/vsix/*.vsix into
# /config/extensions. It reads no environment variables and no config file.

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

# --extensions-dir is NOT optional. The server is launched by the image's own run script with
# `--extensions-dir /config/extensions`; this CLI invocation inherits none of those arguments,
# so without the flag it installs into code-server's DEFAULT directory instead —
# /config/.local/share/code-server/extensions, which the running server never reads. Every
# image rebuild fetches the current version of each extension, and nothing removes the
# previous ones, so that directory grows without bound.
EXTENSIONS_DIR=/config/extensions

# Extensions retired from the image. Dropping one from /opt/vsix does NOT remove it: this
# script only ever installed, so a retired extension stays on the config volume and keeps
# running against a toolchain the image no longer ships. The two below belong to TeX Live;
# LaTeX Workshop in particular would sit there shelling out to a latexmk that is gone.
#
# An explicit list, NOT "remove anything not in /opt/vsix": extensions installed by hand
# through the code-server UI live in the same directory and are not this script's to delete.
# Removing an id from here once it is gone from every volume is safe — uninstalling an absent
# extension is a no-op.
RETIRED_EXTS="
    james-yu.latex-workshop
    tomoki1207.pdf
"

for ext in $RETIRED_EXTS; do
    echo "Removing retired extension: ${ext}"
    /app/code-server/bin/code-server --extensions-dir "${EXTENSIONS_DIR}" --uninstall-extension "${ext}" || true
done

for vsix in /opt/vsix/*.vsix; do
    echo "Installing extension: ${vsix}"
    /app/code-server/bin/code-server --extensions-dir "${EXTENSIONS_DIR}" --install-extension "${vsix}"
done
