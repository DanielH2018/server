"""Error shape of the `homelab_role_label` filter in template_filters/role_label.py (#4505).

A refusal reaches the operator under the task's template path, so it names the filter that
refused. The label behaviour itself is pinned in `test_manifests_prune.py`.

Run: uv run pytest tests/deploy/test_role_label_filter.py
"""

import pytest
from lib.guard_jinja_env import FilterError, repo_filters

from renderer.template_filters.role_label import ROLE_LABEL


@pytest.mark.parametrize(
    ("text", "role"),
    [
        ("kind: ConfigMap\n", ""),
        ("", "svc"),
        ("a: 1\n", "svc"),
        (f"kind: ConfigMap\nmetadata:\n  labels:\n    {ROLE_LABEL}: other\n", "svc"),
    ],
    ids=["no-role", "empty-render", "no-kind", "held-by-another-role"],
)
def test_a_refusal_is_a_filter_error_named_for_its_filter(text, role):
    """A render failure names the filter that refused, through the filter Ansible calls."""
    with pytest.raises(FilterError) as caught:
        repo_filters()["homelab_role_label"](text, role)
    assert str(caught.value).startswith("homelab_role_label: "), str(caught.value)
