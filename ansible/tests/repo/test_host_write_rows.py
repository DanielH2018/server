"""The host-write census refuses a flip whose role still writes a host.

`test_census_rows_roles.py` runs both rows of `_host_write_rows.py` over the tree with their
red and green subjects. The row's allow map is the one part those cannot reach: an entry holds
only while its role is Ansible-owned. This module sits beside the census rather than in it
because that module is at the cap for a test module.

Run: uv run pytest ansible/tests/repo/test_host_write_rows.py
"""

import dataclasses

from _host_write_rows import HOST_WRITE_ALLOW, HOST_WRITES, ansible_owned_allow
from _row_table import check
from lib.k8s_roles import k8s_entries

DOCS = "ansible/roles/k8s/docs/tasks/main.yml"


def test_an_ansible_owned_role_keeps_its_allow_entry():
    assert DOCS in ansible_owned_allow(HOST_WRITE_ALLOW, k8s_entries())


def test_a_flux_owned_role_with_a_host_write_fails_the_census():
    entries = k8s_entries()
    entries["docs"] = {**entries["docs"], "owner": "flux"}
    flipped = dataclasses.replace(
        HOST_WRITES, allow=ansible_owned_allow(HOST_WRITE_ALLOW, entries)
    )
    problems = check(flipped)
    assert any(DOCS in p and "Ensure the built docs site" in p for p in problems), (
        problems
    )
