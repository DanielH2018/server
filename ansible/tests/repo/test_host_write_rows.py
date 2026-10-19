"""The host-write census refuses a flip whose role keeps its allow entry.

`test_census_rows_roles.py` runs both rows of `_host_write_rows.py` over the tree with their
red and green subjects. The row's allow map is the one part those cannot reach: an entry may
exist only while its role is Ansible-owned. Nor can they see whether `_host_script_row.py`'s
registry-gc red case still matches the scale call the script makes. This module sits beside
the census rather than in it because that module is at the cap for a test module.

Run: uv run pytest ansible/tests/repo/test_host_write_rows.py
"""

from _host_script_row import REGISTRY_GC_SCALE
from _host_write_rows import HOST_WRITE_ALLOW, flipped_allow_entries
from _shell_render import rendered_shell_text
from lib.k8s_roles import k8s_entries

REGISTRY = "ansible/roles/k8s/registry/tasks/main.yml"


def test_no_flux_owned_role_keeps_a_host_write_allow_entry():
    assert not flipped_allow_entries(HOST_WRITE_ALLOW, k8s_entries())


def test_a_flip_that_keeps_its_allow_entry_is_flagged_without_a_write():
    """The flip moved registry's writes but left the entry: the entry alone is refused."""
    entries = k8s_entries()
    entries["registry"] = {**entries["registry"], "owner": "flux"}
    assert flipped_allow_entries(HOST_WRITE_ALLOW, entries) == [REGISTRY]


def test_the_registry_gc_red_case_is_the_scale_line_the_script_keeps():
    """A red case proves only as much as its likeness to the call it stands for."""
    script = rendered_shell_text("k8s", "registry", "registry-gc.sh.j2")
    assert REGISTRY_GC_SCALE in script
