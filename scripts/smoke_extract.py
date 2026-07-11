# scripts/smoke_extract.py
"""Extract newly-added container image references from a unified git diff.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2; we pull+run just the new ref.
"""

import re
import sys

# Added line (starts with a single '+', not '+++'), an `image:` key, capture the ref.
_IMAGE_RE = re.compile(r'^\+(?!\+\+)\s*image:\s*["\']?([^\s"\']+)["\']?\s*$')

# Images that can't pass image-smoke's bare `docker run` boot check without their real
# config/creds — either they hard-exit (authelia/couchdb) or they stay up but their image-baked
# HEALTHCHECK never reaches "healthy" within image-smoke's poll window (karakeep). Either way the
# required status check false-fails, blocking the auto-merge / adding manual-override friction that
# their pinned + Renovate-managed bumps are meant to avoid. Skip them; their real post-bump boot is
# covered instead by the gitops-deploy host health gate. Matched by repository, so any tag/digest of
# these is skipped.
_SKIP_BARE_BOOT = frozenset(
    {
        "authelia/authelia",  # fatal without /config/configuration.yml
        "couchdb",  # aborts without COUCHDB_USER/PASSWORD ("Admin Party" refused)
        # boots fine but its baked /api/health check is still "starting" past the ~60s poll window
        # (only reports "unhealthy" at ~t=69s) — false-fails on every Renovate digest-bump PR.
        "ghcr.io/karakeep-app/karakeep",
    }
)


def _repository(ref: str) -> str:
    """The repo part of an image ref, dropping any :tag and @digest. A ':' is the tag
    separator only when it follows the last '/', so a registry:port host isn't mangled."""
    ref = ref.split("@", 1)[0]
    colon = ref.rfind(":")
    if colon > ref.rfind("/"):
        ref = ref[:colon]
    return ref


def extract_changed_images(diff_text: str) -> list[str]:
    seen: list[str] = []
    for line in diff_text.splitlines():
        m = _IMAGE_RE.match(line)
        if not m:
            continue
        ref = m.group(1)
        if _repository(ref) in _SKIP_BARE_BOOT:
            continue
        if ref not in seen:
            seen.append(ref)
    return seen


if __name__ == "__main__":
    images = extract_changed_images(sys.stdin.read())
    for img in images:
        print(img)
