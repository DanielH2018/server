#!/usr/bin/env python3
"""Unit tests for the container dependency-resolution filters in filter_plugins/toposort.py.

These four filters gate the ordering and scope of *every* deploy (see deploy.yml),
so a silent bug here would mis-order or skip services. Pure in/out logic — no Ansible
runtime needed beyond the AnsibleFilterError import.

Lives in ansible/tests/ (not under filter_plugins/) so Ansible's filter-plugin loader
doesn't try to import it as a plugin; the `pythonpath` setting in pyproject.toml puts
filter_plugins/ on sys.path so `import toposort` resolves.

Run: uv run pytest ansible/tests
"""

import pytest
from ansible.errors import AnsibleFilterError

from toposort import (
    build_dep_map,
    dep_closure,
    expand_with_deps,
    toposort_containers,
)


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


# --- toposort_containers ----------------------------------------------------


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


# --- build_dep_map ----------------------------------------------------------


class TestBuildDepMap:
    def _write_deps(self, root, name, role_deps):
        meta = root / "roles" / "containers" / name / "meta"
        meta.mkdir(parents=True, exist_ok=True)
        body = (
            "role_deps:\n" + "".join(f"  - {d}\n" for d in role_deps)
            if role_deps
            else "role_deps: []\n"
        )
        (meta / "deps.yml").write_text(body)

    def test_full_deploy_reads_every_deps_file(self, tmp_path):
        self._write_deps(tmp_path, "a", ["b"])
        self._write_deps(tmp_path, "b", [])
        cl = _containers("a", "b")
        dep_map = build_dep_map(cl, str(tmp_path), ["all"])
        assert dep_map == {"a": ["b"], "b": []}

    def test_tagged_deploy_loads_only_closure(self, tmp_path):
        # a -> b ; c is unrelated. Requesting tag 'a' should load a and b,
        # but leave c at the default empty list (its deps.yml not read).
        self._write_deps(tmp_path, "a", ["b"])
        self._write_deps(tmp_path, "b", [])
        self._write_deps(tmp_path, "c", ["b"])  # would change result if wrongly loaded
        cl = _containers("a", "b", "c")
        dep_map = build_dep_map(cl, str(tmp_path), ["a"])
        assert dep_map["a"] == ["b"]
        assert dep_map["b"] == []
        assert dep_map["c"] == []  # not in closure of 'a' -> not loaded

    def test_missing_deps_file_defaults_empty(self, tmp_path):
        cl = _containers("lonely")
        dep_map = build_dep_map(cl, str(tmp_path), ["all"])
        assert dep_map == {"lonely": []}

    def test_malformed_deps_file_defaults_empty(self, tmp_path):
        meta = tmp_path / "roles" / "containers" / "bad" / "meta"
        meta.mkdir(parents=True)
        (meta / "deps.yml").write_text(": this is : not : valid yaml\n  - [")
        cl = _containers("bad")
        dep_map = build_dep_map(cl, str(tmp_path), ["all"])
        assert dep_map == {"bad": []}

    def test_empty_tags_treated_as_full(self, tmp_path):
        self._write_deps(tmp_path, "a", ["b"])
        self._write_deps(tmp_path, "b", [])
        cl = _containers("a", "b")
        assert build_dep_map(cl, str(tmp_path), []) == {"a": ["b"], "b": []}


# --- dep_closure ------------------------------------------------------------


class TestDepClosure:
    def test_returns_transitive_deps_excluding_requested(self):
        # request 'a' (tag a); a -> b -> c. closure = {b, c}, NOT a.
        cl = _containers("a", "b", "c")
        deps = {"a": ["b"], "b": ["c"], "c": []}
        assert _names(dep_closure(cl, deps, ["a"])) == ["b", "c"]

    def test_no_deps_yields_empty(self):
        cl = _containers("a", "b")
        assert dep_closure(cl, {"a": [], "b": []}, ["a"]) == []

    def test_shared_dep_not_double_counted(self):
        cl = _containers("a", "b", "shared")
        deps = {"a": ["shared"], "b": ["shared"], "shared": []}
        assert _names(dep_closure(cl, deps, ["a"])) == ["shared"]


# --- expand_with_deps -------------------------------------------------------


class TestExpandWithDeps:
    def test_includes_unmet_deps(self):
        cl = _containers("a", "b", "c")
        deps = {"a": ["b"], "b": ["c"], "c": []}
        # nothing running -> requested + all deps, in topo order
        result = expand_with_deps(cl, deps, ["a"], running_names=[])
        assert set(_names(result)) == {"a", "b", "c"}

    def test_skips_already_running_deps(self):
        cl = _containers("a", "b", "c")
        deps = {"a": ["b"], "b": ["c"], "c": []}
        # b and c already running -> only the requested 'a' redeploys
        result = expand_with_deps(cl, deps, ["a"], running_names=["b", "c"])
        assert _names(result) == ["a"]

    def test_requested_always_included_even_if_running(self):
        cl = _containers("a")
        result = expand_with_deps(cl, {"a": []}, ["a"], running_names=["a"])
        assert _names(result) == ["a"]

    def test_partial_running_keeps_the_missing_dep(self):
        cl = _containers("a", "b", "c")
        deps = {"a": ["b"], "b": ["c"], "c": []}
        # b running but c down -> redeploy a and c, skip b
        result = expand_with_deps(cl, deps, ["a"], running_names=["b"])
        assert set(_names(result)) == {"a", "c"}


# --- implicit tags (no `tags:` key => acts as [name]) ------------------------


class TestImplicitTags:
    """host_vars entries no longer carry `tags:` — every tag-matching filter must
    fall back to [name] when the key is absent (explicit tags still override)."""

    def _untagged(self, *names):
        return [{"name": n} for n in names]

    def test_build_dep_map_tagged_deploy_matches_untagged_entry(self, tmp_path):
        helper = TestBuildDepMap()
        helper._write_deps(tmp_path, "a", ["b"])
        helper._write_deps(tmp_path, "b", [])
        cl = self._untagged("a", "b", "c")
        dep_map = build_dep_map(cl, str(tmp_path), ["a"])
        assert dep_map["a"] == ["b"]

    def test_dep_closure_matches_untagged_entry(self):
        cl = self._untagged("a", "b")
        assert _names(dep_closure(cl, {"a": ["b"], "b": []}, ["a"])) == ["b"]

    def test_expand_with_deps_matches_untagged_entry(self):
        cl = self._untagged("a", "b")
        result = expand_with_deps(cl, {"a": ["b"], "b": []}, ["a"], running_names=[])
        assert set(_names(result)) == {"a", "b"}

    def test_explicit_tags_still_override_name(self):
        cl = [{"name": "a", "tags": ["alias"]}]
        # matched via the alias...
        assert _names(expand_with_deps(cl, {"a": []}, ["alias"], [])) == ["a"]
        # ...and NOT via the name once tags are explicit
        assert expand_with_deps(cl, {"a": []}, ["a"], []) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
