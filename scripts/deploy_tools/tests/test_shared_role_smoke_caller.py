#!/usr/bin/env python3
"""`smoke_caller` picks the cheapest render target of a shared role's callers (#3124).

WHY THE PICK IS NOT PINNED BY NAME. `smoke_caller` chooses from the tree, so adding one template
to today's winner moves the answer, and the cost rule is asserted against a synthetic tree, where
the costs are the fixture.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_smoke_caller.py
"""

from shared_role_callers import smoke_caller


def _tree(tmp_path, entries, templates, owners=None):
    """A host_vars + roles pair `smoke_caller` can read.

    Args:
        tmp_path: the directory to build both trees under.
        entries: host name to the service names its `containers_list` declares.
        templates: service name to how many `templates/*.j2` its role renders.
        owners: service name to the `owner` its entry carries; absent means Ansible-owned.

    Returns:
        The `(host_vars, roles)` pair, in `smoke_caller`'s argument order.
    """
    owners = owners or {}
    host_vars, roles = tmp_path / "host_vars", tmp_path / "roles"
    host_vars.mkdir()
    for host, names in entries.items():
        lines = ["containers_list:"]
        for name in names:
            lines.append(f"  - name: {name}")
            lines.append("    platform: k8s")
            if name in owners:
                lines.append(f"    owner: {owners[name]}")
        (host_vars / f"{host}.yml").write_text("\n".join(lines) + "\n")
    for name, count in templates.items():
        (roles / name / "tasks").mkdir(parents=True)
        (roles / name / "tasks" / "main.yml").write_text(
            "- name: Render\n  include_role:\n    name: k8s/manifests\n"
        )
        (roles / name / "templates").mkdir()
        for i in range(count):
            (roles / name / "templates" / f"{i}.yaml.j2").write_text("{}\n")
    return host_vars, roles


def test_the_cheapest_candidate_wins_on_hosts_then_templates_then_name(tmp_path):
    """One host beats two, then fewer manifests, then the name as a stable tie-break."""
    host_vars, roles = _tree(
        tmp_path,
        {"box": ["wide", "cheap", "aardvark", "costly"], "server": ["wide"]},
        {"wide": 1, "cheap": 2, "aardvark": 2, "costly": 9},
    )
    tags = {"wide", "cheap", "aardvark", "costly"}
    assert smoke_caller(tags, host_vars, roles) == "aardvark"
    assert smoke_caller(tags - {"aardvark"}, host_vars, roles) == "cheap"
    assert smoke_caller({"wide", "costly"}, host_vars, roles) == "costly"


def test_a_caller_that_renders_nothing_is_no_candidate(tmp_path):
    """A role that applies its objects some other way proves nothing about the render path."""
    host_vars, roles = _tree(tmp_path, {"box": ["plain"]}, {})
    (roles / "plain" / "tasks").mkdir(parents=True)
    (roles / "plain" / "tasks" / "main.yml").write_text(
        "- name: Call an API\n  debug:\n"
    )
    assert smoke_caller({"plain"}, host_vars, roles) is None


def test_no_candidate_is_none(tmp_path):
    host_vars, roles = _tree(tmp_path, {"box": []}, {})
    assert smoke_caller({"sonarr", "radarr"}, host_vars, roles) is None


def _bento_tree(tmp_path, owners=None):
    """bento-pdf is the cheapest caller, as on the live tree, and littlelink the next."""
    return _tree(
        tmp_path,
        {"box": ["bento-pdf", "littlelink", "sonarr"]},
        {"bento-pdf": 1, "littlelink": 2, "sonarr": 5},
        owners,
    )


def test_an_ansible_owned_cheapest_caller_is_clean(tmp_path):
    host_vars, roles = _bento_tree(tmp_path)
    tags = {"bento-pdf", "littlelink", "sonarr"}
    assert smoke_caller(tags, host_vars, roles) == "bento-pdf"


def test_a_flux_owned_cheapest_caller_is_flagged(tmp_path):
    """`land.sh` drops a Flux-owned tag from its `deploy.sh` call, so the next caller smokes."""
    host_vars, roles = _bento_tree(tmp_path, {"bento-pdf": "flux"})
    tags = {"bento-pdf", "littlelink", "sonarr"}
    assert smoke_caller(tags, host_vars, roles) == "littlelink"
