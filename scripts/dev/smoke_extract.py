# scripts/dev/smoke_extract.py
"""Extract newly-added container image references from a unified git diff.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2, or a `<var>_image: repo:tag[@sha256:...]`
line in a k8s role's defaults/main.yml — the workflow checks just the new ref, from
whichever shape.

EVERY matched ref is emitted. Until 2026-08-21 a `_SKIP_BARE_BOOT` set filtered out 11
repositories that could not survive a bare `docker run`, which also excluded them from the
`docker pull` the workflow ran first — so those refs were verified by nothing at all, not even
that the tag resolved. The workflow's fatal checks no longer execute the image, so nothing
here needs an exception list. See the workflow for what replaced it.
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


def extract_changed_images(diff_text: str) -> list[str]:
    """Every newly-added image ref in `diff_text`, in first-seen order, de-duplicated."""
    seen: list[str] = []
    for line in diff_text.splitlines():
        m = _IMAGE_RE.match(line) or _K8S_DEFAULT_IMAGE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if ref not in seen:
            seen.append(ref)
    return seen


if __name__ == "__main__":
    images = extract_changed_images(sys.stdin.read())
    for img in images:
        print(img)
