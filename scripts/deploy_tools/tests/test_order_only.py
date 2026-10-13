"""Pins for the facts `narrow_lib/order_only.py`'s rule rests on, read from the real tree.

The rule's own accept and reject cases are rows of the case table in
`test_digest_covers.py` and `test_digest_covers_order_only.py`, where a toposort change is
judged end to end.

Run: uv run pytest scripts/deploy_tools/tests/test_order_only.py
"""

from deploy_tools.narrow_lib import filters, order_only
from lib.git_testing import git_out
from lib.repo_paths import REPO

TOPOSORT = "ansible/filter_plugins/toposort.py"


def test_the_real_toposort_registers_the_filters_the_rule_runs():
    """Non-vacuity: the runner calls the three filters by the names the real plugin registers."""
    assert TOPOSORT in order_only.ORDER_ONLY_PLUGINS
    registered = filters.filter_names((REPO / TOPOSORT).read_text(), TOPOSORT)
    assert {
        "build_k8s_dep_map",
        "toposort_containers",
        "filter_by_platform",
    } <= registered


def test_4836s_real_toposort_change_is_order_only():
    """#4836's own `toposort.py` diff discharges when run on the real tree, `ansible` imported.

    The case-table rows run a plugin that imports nothing; this is the one run of the real
    plugin, so a runner that cannot import it would keep every line and still pass them.
    """
    assert order_only.order_only(
        REPO,
        TOPOSORT,
        "8a28ede4dd3f3b56cd11a95f749cf586519c33d3",
        "d6aa1618f746cf025af0f3eb15668f21c27024bf",
    )


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


def test_no_tracked_module_keeps_the_static_write_analysis():
    """The order-only rule runs the sort chain rather than reading it, so the reader is gone (#4842).

    A syntax-reading write check drew a new aliasing bypass in each of two reviews; one left
    beside the dynamic check would be a second rule that can disagree with the first.
    """
    # `[ ]` keeps this pattern from matching its own line.
    out = git_out(
        REPO,
        "grep",
        "-lE",
        r"def[ ]_writes_outside_itself\(",
        "--",
        "*.py",
        check=False,  # no match exits 1
    )
    assert out.split() == []
