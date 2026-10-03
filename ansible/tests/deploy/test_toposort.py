#!/usr/bin/env python3
"""Unit tests for toposort_containers in filter_plugins/toposort.py.

The k8s play in deploy.yml orders every deploy through this filter, so a silent bug here
would mis-order services. Pure in/out logic — no Ansible
runtime needed beyond the AnsibleFilterError import.

Lives in ansible/tests/ (not under filter_plugins/) so Ansible's filter-plugin loader
doesn't try to import it as a plugin; the `pythonpath` setting in pyproject.toml puts
filter_plugins/ on sys.path so `import toposort` resolves.

Run: uv run pytest ansible/tests
"""

import pytest
from ansible.errors import AnsibleFilterError

from toposort import toposort_containers


def _containers(*specs):
    """specs: (name, [tags]) or bare name -> list of container dicts."""
    out = []
    for s in specs:
        if isinstance(s, tuple):
            name, tags = s
        else:
            name, tags = s, [s]
        out.append({"name": name, "tags": tags})
    return out


def _names(containers):
    return [c["name"] for c in containers]


class TestToposort:
    def test_linear_chain_orders_deps_first(self):
        # a depends on b, b depends on c  =>  c, b, a
        cl = _containers("a", "b", "c")
        deps = {"a": ["b"], "b": ["c"], "c": []}
        assert _names(toposort_containers(cl, deps)) == ["c", "b", "a"]

    def test_diamond_orders_root_first_and_keeps_tie_order(self):
        # d -> (b, c) -> a.  a first, then b,c in original list order, then d.
        cl = _containers("d", "b", "c", "a")
        deps = {"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}
        assert _names(toposort_containers(cl, deps)) == ["a", "b", "c", "d"]

    def test_independent_nodes_preserve_input_order(self):
        cl = _containers("x", "y", "z")
        assert _names(toposort_containers(cl, {})) == ["x", "y", "z"]

    def test_ties_within_level_are_stable(self):
        # both b and c become free at the same level once a is placed;
        # they must come out in original list order (c before b here).
        cl = _containers("a", "c", "b")
        deps = {"c": ["a"], "b": ["a"], "a": []}
        assert _names(toposort_containers(cl, deps)) == ["a", "c", "b"]

    def test_cycle_raises(self):
        cl = _containers("a", "b")
        deps = {"a": ["b"], "b": ["a"]}
        with pytest.raises(AnsibleFilterError) as exc:
            toposort_containers(cl, deps)
        assert "cycle" in str(exc.value).lower()

    def test_dep_absent_from_list_is_ignored(self):
        # 'ghost' isn't a container; 'a' should still sort with in-degree 0.
        cl = _containers("a")
        assert _names(toposort_containers(cl, {"a": ["ghost"]})) == ["a"]

    def test_returns_original_objects(self):
        cl = _containers("a", "b")
        result = toposort_containers(cl, {"a": ["b"], "b": []})
        assert result[0] is cl[1] and result[1] is cl[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
