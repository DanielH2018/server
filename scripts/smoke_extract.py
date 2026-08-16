# scripts/smoke_extract.py
"""Extract newly-added container image references from a unified git diff.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2, or a `<var>_image: repo:tag[@sha256:...]`
line in a k8s role's defaults/main.yml — we pull+run just the new ref, from whichever shape.
"""

import re
import sys

# Added line (starts with a single '+', not '+++'), an `image:` key, capture the ref.
_IMAGE_RE = re.compile(r'^\+(?!\+\+)\s*image:\s*["\']?(?P<ref>[^\s"\']+)["\']?\s*$')

# Same, for a k8s role default's `<var>_image: repo:tag[@sha256:...]` — optionally quoted,
# optionally trailing a YAML comment. A Jinja-templated value (registry-built images like
# n8n_k8s_image: "{{ k8s_registry_pull_host }}/n8n:latest") has a space inside the quotes, so the
# bare-ref group can't reach the closing quote/comment and the whole line fails to match — those
# are deliberately not a smoke target (no stable upstream ref to pull; see REGISTRY_BUILT_IMAGES
# in scripts/test_renovate_managers.py).
_K8S_DEFAULT_IMAGE_RE = re.compile(
    r"""^\+(?!\+\+)\s*\w+_image:\s*(?P<quote>["'])?(?P<ref>[^\s"']+)(?P=quote)?(?:\s+\#.*)?$"""
)

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
        # exits immediately with `failed to create store: unknown backend ""` — the storage
        # backend has no default, so it cannot boot without its bind-mounted config.yaml
        "grafana/tempo",
        # boots fine but its baked /api/health check is still "starting" past the ~60s poll window
        # (only reports "unhealthy" at ~t=69s) — false-fails on every Renovate digest-bump PR.
        "ghcr.io/karakeep-app/karakeep",
        # needs NET_ADMIN + a mounted wg config; in the cluster it runs only as qbittorrent's
        # sidecar initContainer, never standalone.
        "lscr.io/linuxserver/wireguard",
        # interactive world-selection prompt on a bare boot (no world files mounted).
        "beardedio/terraria",
        # exits with "Either CLOUDFLARE_API_TOKEN or CLOUDFLARE_API_TOKEN_FILE must be set"
        # (observed run 31731079802) — config-required, same class as authelia/couchdb.
        "favonia/cloudflare-ddns",
        # Chrome's setuid sandbox is incompatible with this smoke's own
        # --security-opt=no-new-privileges (observed run 31731079802); the karakeep
        # deployment runs it with --no-sandbox, which a bare boot doesn't.
        "zenika/alpine-chrome",
        # master-web boots but its healthcheck needs the influxdb sidecar (observed run
        # 31731079802). Repository-level match also skips master-collector.
        "ghcr.io/analogj/scrutiny",
        # Entrypoint is the `uv` binary, not an interpreter: with no arguments it prints its
        # help and exits 1 (observed run 31972614283). That breaks the boot step's stated
        # assumption that a base image "runs a REPL that exits 0 on EOF" — true of
        # python:*-alpine, false here — so a bare boot of this image can never pass, at any
        # tag. Nothing is lost by skipping: the three karakeep manifests that use it all
        # override `command:`, so the bare entrypoint is not what runs in the cluster.
        "ghcr.io/astral-sh/uv",
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
        m = _IMAGE_RE.match(line) or _K8S_DEFAULT_IMAGE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if _repository(ref) in _SKIP_BARE_BOOT:
            continue
        if ref not in seen:
            seen.append(ref)
    return seen


if __name__ == "__main__":
    images = extract_changed_images(sys.stdin.read())
    for img in images:
        print(img)
