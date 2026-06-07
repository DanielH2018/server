# scripts/smoke_extract.py
"""Extract newly-added container image references from a unified git diff.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2; we pull+run just the new ref.
"""
import re
import sys

# Added line (starts with a single '+', not '+++'), an `image:` key, capture the ref.
_IMAGE_RE = re.compile(r'^\+(?!\+\+)\s*image:\s*["\']?([^\s"\']+)["\']?\s*$')


def extract_changed_images(diff_text: str) -> list[str]:
    seen: list[str] = []
    for line in diff_text.splitlines():
        m = _IMAGE_RE.match(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


if __name__ == "__main__":
    images = extract_changed_images(sys.stdin.read())
    for img in images:
        print(img)
