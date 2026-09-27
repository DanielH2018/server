#!/usr/bin/env python3
"""`deploy.sh --tags <shared role>` deploys every role that runs it (issue #2704).

A shared k8s role has no `containers_list` entry, so Ansible selects nothing for its name and
exits 0. PR #2701 changed only `volume-snapshot/tasks/claim.yml` and had to wait for the next
full deploy. `deploy_run.expand_shared_roles` swaps the name for its callers' tags before any
gate runs, and these cases drive it against a throwaway repo whose role graph they write.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_expands_shared_roles.py
"""

from _deploy_sh_fakes import make_snapshot_repo, run_front_half

_INCLUDE_HELPER = (
    "- name: Run the helper\n  ansible.builtin.include_role:\n    name: k8s/helper\n"
)


def _repo(tmp_path):
    """A repo where `alpha` and `beta` (both declared) include the tag-less `helper`."""
    repo = make_snapshot_repo(tmp_path / "repo")
    for caller in ("alpha", "beta"):
        tasks = repo / "ansible" / "roles" / "k8s" / caller / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "main.yml").write_text(_INCLUDE_HELPER)
    return repo


def _gate_tags(calls, gate):
    return next(c for c in calls if c[0] == gate)[1]


def test_a_shared_role_tag_becomes_its_callers_is_clean(tmp_path, monkeypatch):
    """Every gate and the playbook argv see the callers, never the tag-less name."""
    code, calls = run_front_half(
        monkeypatch, _repo(tmp_path), ["--tags", "helper", "--check"]
    )
    assert code is None, calls
    assert _gate_tags(calls, "staleness") == ("alpha", "beta")
    assert _gate_tags(calls, "validate") == ("alpha", "beta")
    argv = next(c for c in calls if c[0] == "deploy")[1]
    assert [a for a in argv if "tags" in a] == ["--tags=alpha,beta"], argv


def test_a_name_no_role_runs_is_flagged(tmp_path, monkeypatch):
    """Left as typed, so the tag validation refuses it by name rather than deploying nothing."""
    _, calls = run_front_half(
        monkeypatch, _repo(tmp_path), ["--tags", "nosuchrole"], validate=2
    )
    assert _gate_tags(calls, "validate") == ("nosuchrole",)


def test_a_declared_tag_beside_a_shared_role_is_kept_once(tmp_path, monkeypatch):
    _, calls = run_front_half(
        monkeypatch, _repo(tmp_path), ["--tags=beta,helper,gamma-not-declared"]
    )
    assert _gate_tags(calls, "validate") == ("beta", "alpha", "gamma-not-declared")


def test_a_build_only_caller_brings_the_role_that_rolls_its_image(
    tmp_path, monkeypatch
):
    """`n8n-images` builds what `n8n` deploys, so expanding to it alone would roll nothing."""
    repo = _repo(tmp_path)
    tasks = repo / "ansible" / "roles" / "k8s" / "n8n-images" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(_INCLUDE_HELPER.replace("helper", "builder"))
    host_vars = repo / "ansible" / "inventory" / "host_vars" / "daniel-box.yml"
    host_vars.write_text(host_vars.read_text() + "  - { name: n8n-images }\n")
    _, calls = run_front_half(monkeypatch, repo, ["--tags", "builder"])
    assert _gate_tags(calls, "validate") == ("n8n-images", "n8n")
