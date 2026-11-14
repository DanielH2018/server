# scripts/dev/smoke_extract.py
"""Extract the container image references a unified git diff adds and does not also remove.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2, or a `<var>_image: repo:tag[@sha256:...]`
line in a k8s role's defaults/main.yml — the workflow checks just the new ref, from
whichever shape. A bare `image: name` is no ref: a role's `manifests_extra_rollouts` entry names
the image it built that way.

A changed image is a ref the added lines carry that no removed line carries. Moving a defaults
file, reformatting it or editing a comment on an image line removes and re-adds the same ref,
so it selects nothing, whether or not git pairs the move as a rename.

EVERY matched ref is emitted. The workflow's fatal checks do not execute the image, so nothing
here needs an exception list.
"""

import re
import sys
from collections.abc import Sequence
from pathlib import Path

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import answer_help

# An added or removed line (a single '+' or '-', not a '+++' or '---' header), an `image:` key,
# capture a ref with a tag or digest.
_IMAGE_RE = re.compile(
    r'^(?P<sign>\+(?!\+\+)|-(?!--))\s*image:\s*["\']?(?P<ref>[^\s"\':]+:[^\s"\']+)["\']?\s*$'
)

# Same, for a k8s role default's `<var>_image: repo:tag[@sha256:...]` — optionally quoted,
# optionally trailing a YAML comment. A Jinja-templated value (registry-built images like
# n8n_k8s_image: "{{ k8s_registry_pull_host }}/n8n:latest") has a space inside the quotes, so the
# bare-ref group can't reach the closing quote/comment and the whole line fails to match — those
# are deliberately not a smoke target (no stable upstream ref to pull; see REGISTRY_BUILT_IMAGES
# in scripts/test_renovate_managers.py).
_K8S_DEFAULT_IMAGE_RE = re.compile(
    r"""^(?P<sign>\+(?!\+\+)|-(?!--))\s*\w+_image:\s*(?P<quote>["'])?(?P<ref>[^\s"']+)(?P=quote)?(?:\s+\#.*)?$"""
)


def extract_changed_images(diff_text: str) -> list[str]:
    """Every image ref `diff_text` adds and does not remove, in first-seen order, de-duplicated."""
    added: list[str] = []
    removed: set[str] = set()
    for line in diff_text.splitlines():
        m = _IMAGE_RE.match(line) or _K8S_DEFAULT_IMAGE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if m.group("sign") == "-":
            removed.add(ref)
        elif ref not in added:
            added.append(ref)
    return [ref for ref in added if ref not in removed]


def main(argv: Sequence[str] = ()) -> None:
    """Print every image ref the diff on stdin adds and does not remove, one per line."""
    # Before the read: the diff arrives on stdin, so `--help` would hang on a terminal
    # rather than answer.
    answer_help(__doc__, list(argv))
    for img in extract_changed_images(sys.stdin.read()):
        print(img)


if __name__ == "__main__":
    main(sys.argv[1:])
