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


def test_a_change_to_the_real_dep_map_that_writes_only_its_locals_stays_order_only():
    """The write check still reads every callee the real `build_k8s_dep_map` reaches (#4842).

    Its body hands `playbook_dir` to `os.path.join` and `_role_renders_traefik_crd`, which
    reaches `os.listdir`, `open` and `fh.read()`. A rule that read any of them as a write
    would refuse every change here, the #4836 shape included, while the toy case table stayed
    green. The same edit through `operator.setitem` on an entry is the red half.
    """
    path = "ansible/filter_plugins/toposort.py"
    real = (REPO / path).read_text()
    anchor = "        dep_map[name] = sorted(deps)\n"
    assert anchor in real

    def with_body(line: str) -> str:
        return (
            real.replace(
                "\nclass FilterModule", "\nLAST = {'x'}\n\n\nclass FilterModule"
            )
            .replace("import heapq\n", "import heapq\nimport operator\n")
            .replace(anchor, line + anchor)
        )

    adds = "        if name in LAST:\n            deps.update(n for n in dep_map if n not in LAST)\n"
    assert order_only.order_only(path, real, with_body(adds))
    writes = "        operator.setitem(c, 'use_authelia', False)\n"
    assert not order_only.order_only(path, real, with_body(writes))
