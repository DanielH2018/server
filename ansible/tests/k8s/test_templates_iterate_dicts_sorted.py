"""No tracked Jinja template iterates a dict with `.items()`; a manifest loop uses `dictsort`.

A `{% for k, v in d.items() %}` in a manifest renders the dict in insertion order, so a
reorder of the source — a `defaults/main.yml` edit, a caller listing its `image_builder_context`
in a different order, a group_vars tidy — changes the rendered bytes with no change in meaning.
The manifests role restarts every workload a render change could belong to
(`roles/k8s/manifests/CLAUDE.md`, "The restart is skipped for a workload the apply itself
rolled"), and image-builder's byte-identical gate rebuilds the image. `| dictsort` renders
the same dict the same way whatever order it was written in; `release_stamp.yml`'s
`dictsort | to_json | hash('sha256')` is the in-tree pattern (#2157).

Every tracked `.j2` is scanned, not only `roles/k8s/**/templates/`: a `templates/config/` file
is embedded in a ConfigMap through `lookup('template')`, so its key order is manifest bytes
too, and a setup or Pi template carries the same hazard into a file diff.

Run: uv run pytest ansible/tests/k8s/test_templates_iterate_dicts_sorted.py
"""

import re
import subprocess

from _helpers import REPO

# `.items()` anywhere on a line, unless that line also sorts — `| dictsort` is the form this
# repo uses, and `.items() | sort` is the same guarantee spelled longer.
UNSORTED_ITEMS = re.compile(r"\.items\(\)(?![^\n]*\bsort\b)")

# Templates the census must contain, so a glob that silently narrows fails by name rather
# than by a count moving (see "A check that finds its own subject by pattern" in CLAUDE.md).
KNOWN_TEMPLATES = frozenset(
    {
        "ansible/roles/k8s/claude-otel/templates/00-namespace.yaml.j2",
        "ansible/roles/k8s/image-builder/templates/context-configmap.yaml.j2",
        "ansible/roles/k8s/uptime-kuma/templates/status-page-sync-configmap.yaml.j2",
        "ansible/templates/checksum-annotation.yml.j2",
    }
)


def _tracked_templates() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.j2"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def test_the_scan_finds_the_templates_it_exists_to_check():
    """Without this, the guard below passes vacuously on an empty or narrowed file list."""
    found = set(_tracked_templates())
    assert KNOWN_TEMPLATES <= found, sorted(KNOWN_TEMPLATES - found)
    assert len(found) >= 400


def test_no_tracked_template_iterates_a_dict_in_insertion_order():
    offenders = [
        f"{rel}:{n}"
        for rel in _tracked_templates()
        for n, line in enumerate(
            (REPO / rel).read_text(errors="replace").splitlines(), 1
        )
        if UNSORTED_ITEMS.search(line)
    ]
    assert not offenders, (
        f"{offenders} iterate a dict with .items(), which renders it in insertion order. "
        f"A reorder of the source then changes the rendered bytes, which restarts the "
        f"workload and rebuilds an image-builder context for no change in meaning. "
        f"Use `{{% for k, v in d | dictsort %}}`."
    )


def test_the_pattern_rejects_items_and_accepts_dictsort():
    """Red-proof pair for UNSORTED_ITEMS itself."""
    assert UNSORTED_ITEMS.search("{% for key, value in k8s_psa_labels.items() %}")
    assert not UNSORTED_ITEMS.search(
        "{% for key, value in k8s_psa_labels | dictsort %}"
    )
    assert not UNSORTED_ITEMS.search("{% for key, value in labels.items() | sort %}")
