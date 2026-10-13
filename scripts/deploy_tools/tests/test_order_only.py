"""Pins for the facts `narrow_lib/order_only.py`'s rule rests on, read from the real tree.

The rule's own accept and reject cases are rows of the case table in
`test_digest_covers.py`, where a toposort change is judged end to end.

Run: uv run pytest scripts/deploy_tools/tests/test_order_only.py
"""

import pytest

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


TOPOSORT = "ansible/filter_plugins/toposort.py"
ANCHOR = "        dep_map[name] = sorted(deps)\n"


def _real_with(body: str, helper: str = "") -> tuple[str, str]:
    """The real plugin, and it with `body` run on each entry `c` of `build_k8s_dep_map`.

    `helper` is a top-level statement placed before `FilterModule`.
    """
    real = (REPO / TOPOSORT).read_text()
    assert ANCHOR in real
    lines = "".join(f"        {line}\n" for line in body.split("\n"))
    after = real.replace(ANCHOR, lines + ANCHOR)
    if helper:
        after = after.replace(
            "\nclass FilterModule", f"\n{helper}\n\n\nclass FilterModule"
        )
    return real, after


@pytest.mark.parametrize(
    ("helper", "body"),
    [
        pytest.param(
            "LAST = {'x'}",
            "if name in LAST:\n    deps.update(n for n in dep_map if n not in LAST)",
            id="4836-shape",
        ),
        pytest.param(
            "", "open(os.path.join(playbook_dir, name)).read()", id="read-open"
        ),
        pytest.param("", "h = {}\nh[name] = c", id="one-level-store-into-a-holder"),
    ],
)
def test_a_change_to_the_real_dep_map_that_writes_only_its_locals_stays_order_only(
    helper, body
):
    """The write check still reads every callee the real `build_k8s_dep_map` reaches (#4842).

    Its body hands `playbook_dir` to `os.path.join` and `_role_renders_traefik_crd`, which
    reaches `os.listdir`, `open` and `fh.read()`. A rule that read any of them as a write
    would refuse every change here, the #4836 shape included, while the toy case table in
    `test_digest_covers.py` stayed green.
    """
    assert order_only.order_only(TOPOSORT, *_real_with(body, helper))


@pytest.mark.parametrize(
    ("helper", "body"),
    [
        pytest.param(
            "import operator",
            "operator.setitem(c, 'use_authelia', False)",
            id="operator",
        ),
        # A decorator or a base runs code the check never walks.
        pytest.param(
            "import functools\n\n\n@functools.cache\ndef _h(c):\n    pass",
            "_h(c)",
            id="decorated-def",
        ),
        pytest.param(
            "import collections\n\n\nclass _h(collections.UserDict):\n    pass",
            "_h(c)",
            id="class-with-a-base",
        ),
        pytest.param("_X = dict.update", "_X(c, use_authelia=False)", id="opaque-name"),
        # The callee itself carries the entry.
        pytest.param("", "f = c.update\nf(use_authelia=False)", id="aliased-method"),
        pytest.param(
            "", "f = c.__setitem__\nf('use_authelia', False)", id="aliased-dunder"
        ),
        pytest.param(
            "", "for f in [c.update]:\n    f(use_authelia=False)", id="loop-alias"
        ),
        pytest.param(
            "", "[f(use_authelia=False) for f in [c.update]]", id="comprehension-alias"
        ),
        pytest.param(
            "", "(f := c.__setitem__)\nf('use_authelia', False)", id="walrus-alias"
        ),
        # An entry stored in a local container, then written through it.
        pytest.param(
            "", "h = {}\nh['e'] = c\nh['e']['use_authelia'] = False", id="dict-holder"
        ),
        pytest.param(
            "", "h = []\nh.append(c)\nh[0]['use_authelia'] = False", id="list-holder"
        ),
        pytest.param(
            "",
            "h = []\nh.append(c)\nh[0].update(use_authelia=False)",
            id="holder-mutator",
        ),
        pytest.param(
            "",
            "h = {}\nh['e'] = c\nfor e in h.values():\n    e['x'] = 1",
            id="holder-read",
        ),
        pytest.param(
            "",
            "h = {'d': []}\nh['d'] = c['depends_on']\nh['d'] += ['x']",
            id="holder-augassign",
        ),
        # The write case of each read allowance.
        pytest.param("", "sorted(containers_list, key=dict.clear)", id="sorted-key"),
        pytest.param(
            "from shutil import rmtree as open",
            "open(c['name'])",
            id="shadowed-builtin",
        ),
        pytest.param("", "open(c['name'], 'w')", id="open-write"),
        pytest.param("", "open(c['name'], mode='a+')", id="open-append"),
        pytest.param(
            "import shutil",
            "shutil.copy(c['name'], 'x')",
            id="read-method-name-on-a-module",
        ),
    ],
)
def test_a_change_that_can_reach_an_entry_keeps_the_line(helper, body):
    """Each change hands an entry, or a path from one, to code that may write it (#4842)."""
    assert not order_only.order_only(TOPOSORT, *_real_with(body, helper))
