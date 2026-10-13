"""Pins for the facts `narrow_lib/order_only.py`'s rule rests on, read from the real tree.

The rule's own accept and reject cases are rows of the case table in
`test_digest_covers.py`, where a toposort change is judged end to end.

Run: uv run pytest scripts/deploy_tools/tests/test_order_only.py
"""

from deploy_tools.narrow_lib import order_only
from lib.git_testing import git_out
from lib.repo_paths import REPO


def test_the_real_toposort_registers_the_order_only_filters():
    """Non-vacuity: the table names filters the real plugin still registers by those names."""
    text = (REPO / "ansible/filter_plugins/toposort.py").read_text()
    registered = order_only._registered(text)
    order = order_only.ORDER_ONLY_FILTERS["ansible/filter_plugins/toposort.py"]
    assert order < set(registered)
    assert "filter_by_platform" in registered


def test_the_play_reads_the_ordering_filters_only_to_order_itself():
    """`ORDER_ONLY_FILTERS` holds while these two `set_fact`s are the filters' only callers.

    A new caller, or a role reading the dependency map, would let a change to them reach
    something other than deploy order, and this goes red on it.
    """
    out = git_out(
        REPO,
        "grep",
        "-hE",
        r"(\|\s*|['\"])(build_k8s_dep_map|toposort_containers)\b|_k8s_container_deps_map",
        "--",
        "ansible/",
        ":!ansible/tests/",
        ":!ansible/filter_plugins/",
    )
    calls = {
        line.strip() for line in out.splitlines() if not line.strip().startswith("#")
    }
    assert calls == {
        '_k8s_container_deps_map: "{{ containers_list | build_k8s_dep_map(playbook_dir) }}"',
        'containers_list: "{{ containers_list | toposort_containers(_k8s_container_deps_map) }}"',
    }
