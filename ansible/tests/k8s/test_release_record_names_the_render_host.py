"""The release record names the inventory host that rendered it (#2532).

`release_stamp.yml` identifies a release by the sha256 of the RENDERED manifests, on the
grounds that one commit can render differently on two hosts. That reasoning only closes if
the record says which host rendered it: read anywhere but the node that wrote it, or compared
against a fresh render, a hostless record states a fact nothing can reproduce.

The value matters as much as the key. `inventory_hostname` is the host whose `host_vars`
layered into the render, which is what a reader reproducing that render needs; a presence-only
assertion would stay green against a typo'd expression.

Run: uv run pytest ansible/tests/k8s/test_release_record_names_the_render_host.py
"""

from _helpers import task_named
from _release_expectation import STAMP


def test_the_record_names_the_inventory_host():
    write = task_named(STAMP, "Write the release record")
    assert (
        write["vars"]["manifests_release_record"]["host"] == "{{ inventory_hostname }}"
    )
