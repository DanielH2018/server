#!/bin/bash
# Install proprietary extensions (downloaded at image build time) into the
# user config volume on each container start.
#
# Usage: extensions.sh (no arguments)
# Runs as /custom-cont-init.d/10-extensions.sh in the code-server image, on every start of the
# container. It uninstalls each id in RETIRED_EXTS, then installs each
# /opt/vsix/<publisher>.<name>@<version>.vsix whose id and version /config/extensions does not
# already hold. It exits 1 when an install fails. CODE_SERVER and VSIX_DIR override the CLI
# path and the VSIX directory, for tests.

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
CODE_SERVER="${CODE_SERVER:-/app/code-server/bin/code-server}"
VSIX_DIR="${VSIX_DIR:-/opt/vsix}"

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
    "${CODE_SERVER}" --extensions-dir "${EXTENSIONS_DIR}" --uninstall-extension "${ext}" || true
done

# code-server refuses to reinstall a version it already holds ("Please restart VS Code before
# reinstalling") and exits non-zero, so an unchanged VSIX is skipped rather than installed.
# Each file name is the `<id>@<version>` line --show-versions prints; ids are case-insensitive.
installed="$("${CODE_SERVER}" --extensions-dir "${EXTENSIONS_DIR}" --list-extensions --show-versions)"

failed=0
for vsix in "${VSIX_DIR}"/*.vsix; do
    pinned="$(basename "${vsix}" .vsix)"
    if grep -qFxi -- "${pinned}" <<<"${installed}"; then
        echo "Already installed: ${pinned}"
        continue
    fi
    echo "Installing extension: ${vsix}"
    "${CODE_SERVER}" --extensions-dir "${EXTENSIONS_DIR}" --install-extension "${vsix}" || failed=1
done
exit "${failed}"
