"""`ansible_bool` and the filter registration justify `| bool` without an extra-vars path (#8182).

Run: uv run pytest scripts/lib/tests/test_ansible_bool_rationale.py
"""

import re

import pytest

from lib import ansible_jinja_env as aje

# `-e var=...`, `--extra-vars` or "extra vars": a playbook's command-line variables, which no
# render path at HEAD has.
_EXTRA_VARS = re.compile(r"(?<![\w-])-e\s+\w+=|extra[- _]vars", re.IGNORECASE)


@pytest.mark.parametrize("name", ["ansible_bool", "register_ansible_filters"])
def test_bool_rationale_cites_no_extra_vars_path(name: str):
    doc = getattr(aje, name).__doc__ or ""
    assert not _EXTRA_VARS.search(doc), _EXTRA_VARS.findall(doc)
