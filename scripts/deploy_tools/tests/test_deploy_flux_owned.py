"""`deploy.sh` refuses, drops and marks a tag that is not Ansible's to deploy.

A k8s entry carrying `owner` is applied by Flux through `promote`, or is mid-handback, so no
Ansible path may apply it (docs/target-architecture/deploy/d2-first-leaf.md, *What each entry
point does with a tag that is not Ansible-owned*). These cases run the front half against a
throwaway repo whose `containers_list` declares one such entry, `bento`, beside the
Ansible-owned ones `make_snapshot_repo` writes.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_flux_owned.py
"""

from functools import partial

import pytest
from _deploy_sh_fakes import make_snapshot_repo, run_front_half

from deploy_tools.deploy_lib import run as deploy_run
from deploy_tools.deploy_lib import tags as deploy_tags
from deploy_tools.deploy_lib.flags import NothingToDeploy
from lib.exit_codes import DEPLOY_FLUX_OWNED
from lib.git_testing import git

HOST_FILE = "ansible/inventory/host_vars/daniel-box.yml"

_INCLUDE_HELPER = (
    "- name: Run the helper\n  ansible.builtin.include_role:\n    name: k8s/helper\n"
)


def _repo(tmp_path, owner="flux", helper_callers=()):
    """A repo declaring `bento` with `owner`, and the given roles including `k8s/helper`."""
    repo = make_snapshot_repo(tmp_path / "repo")
    host = repo / HOST_FILE
    host.write_text(
        host.read_text() + f"  - {{ name: bento, platform: k8s, owner: {owner} }}\n"
    )
    for caller in helper_callers:
        tasks = repo / "ansible" / "roles" / "k8s" / caller / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "main.yml").write_text(_INCLUDE_HELPER)
    return repo


def _deployed(calls):
    return [c[1] for c in calls if c[0] == "deploy"]


# -- deploy.sh --tags T --------------------------------------------------------------------


@pytest.mark.parametrize("owner", ["flux", "released"])
def test_a_typed_tag_that_is_not_ansible_owned_is_flagged(
    owner, tmp_path, monkeypatch, capsys
):
    """Exit 5 for the whole list, before the locked half takes any lock."""
    code, calls = run_front_half(
        monkeypatch, _repo(tmp_path, owner), ["--tags", "alpha,bento"]
    )
    assert code == DEPLOY_FLUX_OWNED
    assert _deployed(calls) == []
    err = capsys.readouterr().err
    assert (
        f"`bento` is {'applied by Flux' if owner == 'flux' else 'mid-handback'}" in err
    )
    assert f"owner: {owner}" in err and "No Ansible path may apply `bento`" in err


def test_an_ansible_owned_tag_beside_one_is_clean(tmp_path, monkeypatch):
    code, calls = run_front_half(monkeypatch, _repo(tmp_path), ["--tags", "alpha"])
    assert code is None
    assert _deployed(calls) == [["in-process", "alpha"]]


@pytest.mark.parametrize("flag", ["--skip-tag-check", "--dry-run"])
def test_neither_the_tag_bypass_nor_a_dry_run_skips_the_refusal_is_flagged(
    flag, tmp_path, monkeypatch
):
    code, calls = run_front_half(
        monkeypatch, _repo(tmp_path), ["--tags", "bento", flag]
    )
    assert code == DEPLOY_FLUX_OWNED
    assert _deployed(calls) == []


def test_at_reads_ownership_at_the_commit_it_deploys_is_flagged(tmp_path, monkeypatch):
    """A landing's `--at` commit sets `owner` before the checkout has it."""
    repo = _repo(tmp_path)
    git(repo, "commit", "-qam", "own bento", "--no-gpg-sign")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    host = repo / HOST_FILE
    host.write_text(host.read_text() + "  - { name: bento, platform: k8s }\n")

    assert run_front_half(monkeypatch, repo, ["--tags", "bento"])[0] is None
    code, _ = run_front_half(monkeypatch, repo, ["--at", sha, "--tags", "bento"])
    assert code == DEPLOY_FLUX_OWNED


def test_an_owner_outside_the_known_values_refuses_is_flagged(tmp_path, monkeypatch):
    """Read as Ansible-owned, a misspelt owner would hand the tag to both writers."""
    code, calls = run_front_half(
        monkeypatch, _repo(tmp_path, owner="fluxx"), ["--tags", "alpha"]
    )
    assert code == 1
    assert _deployed(calls) == []


# -- deploy.sh --tags T -e target=<host> ---------------------------------------------------


def _two_host_repo(tmp_path):
    """daniel-box applies `wg-easy` through Flux; daniel-pi lists a Docker `wg-easy` (#5815)."""
    repo = make_snapshot_repo(tmp_path / "repo")
    box = repo / HOST_FILE
    box.write_text(
        box.read_text() + "  - { name: wg-easy, platform: k8s, owner: flux }\n"
    )
    (repo / "ansible/inventory/host_vars/daniel-pi.yml").write_text(
        "containers_list:\n  - { name: wg-easy, platform: docker }\n"
    )
    return repo


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (["-e", "target=daniel-pi"], None),
        (["--extra-vars", "x=1 target=daniel-pi"], None),
        (["-e", "target=daniel-box"], DEPLOY_FLUX_OWNED),
        ([], DEPLOY_FLUX_OWNED),
    ],
)
def test_a_tag_both_hosts_list_is_refused_only_for_the_host_that_flux_owns(
    extra, code, tmp_path, monkeypatch
):
    repo = _two_host_repo(tmp_path)
    got, calls = run_front_half(monkeypatch, repo, ["--tags", "wg-easy", *extra])
    assert got == code
    assert bool(_deployed(calls)) == (code is None)


# -- deploy.sh --changed -------------------------------------------------------------------


def _diff(roles):
    paths = [f"ansible/roles/k8s/{role}/templates/deployment.yaml.j2" for role in roles]
    return lambda ref, cwd: paths


def _changed(monkeypatch, repo, roles):
    """The front half with the real `--changed` derivation over a diff touching `roles`."""
    derive = partial(deploy_run.run_changed, git_diff=_diff(roles))
    return run_front_half(monkeypatch, repo, ["--changed"], changed=derive)


def test_changed_deriving_only_a_flux_tag_ends_as_nothing_to_deploy_is_flagged(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path)
    code, calls = _changed(monkeypatch, repo, ["bento"])
    assert code == 0
    assert _deployed(calls) == []
    assert "--changed: left out `bento` is applied by Flux" in capsys.readouterr().err
    # Exit 0 alone reads as a deploy; the verdict line tells the two apart.
    plan = deploy_run.Plan(repo_root=repo, changed=True, skip_staleness_check=True)
    tools = deploy_run.Tools(
        changed=partial(deploy_run.run_changed, git_diff=_diff(["bento"]))
    )
    with pytest.raises(NothingToDeploy):
        deploy_run.derive_changed_tags(plan, [], tools)


def test_landings_changed_call_keeps_a_flux_tag_is_clean(tmp_path, capsys):
    """land.sh runs `deploy_tags.py changed` with no `skip`; D2.3 owns its split."""
    status = deploy_tags.changed(
        "origin/master", cwd=_repo(tmp_path), git_diff=_diff(["bento"])
    )
    assert status == 0
    assert capsys.readouterr().out == "bento\n"


def test_changed_keeps_the_ansible_owned_tags_beside_it_is_clean(tmp_path, monkeypatch):
    code, calls = _changed(monkeypatch, _repo(tmp_path), ["bento", "alpha"])
    assert code is None
    assert _deployed(calls) == [["in-process", "alpha"]]


# -- deploy.sh --tags manifests ------------------------------------------------------------


def test_a_shared_role_expansion_drops_its_flux_caller_is_clean(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path, helper_callers=("alpha", "beta", "bento"))
    code, calls = run_front_half(monkeypatch, repo, ["--tags", "helper"])
    assert code is None
    assert _deployed(calls) == [["in-process", "alpha,beta"]]
    assert "left `bento` out of `helper`'s callers" in capsys.readouterr().err


def test_an_expansion_the_drop_empties_deploys_nothing_is_flagged(
    tmp_path, monkeypatch
):
    """Exit 0 with no deploy: an empty `--tags` would be a full run."""
    repo = _repo(tmp_path, helper_callers=("bento",))
    code, calls = run_front_half(monkeypatch, repo, ["--tags", "helper"])
    assert code == 0
    assert _deployed(calls) == []
    with pytest.raises(NothingToDeploy):
        deploy_run.expand_shared_roles(deploy_run.Plan(repo_root=repo, tags=["helper"]))


def test_a_typed_flux_tag_beside_its_shared_role_still_refuses_is_flagged(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, helper_callers=("alpha", "bento"))
    code, _ = run_front_half(monkeypatch, repo, ["--tags", "helper,bento"])
    assert code == DEPLOY_FLUX_OWNED


# -- deploy.sh --list-services -------------------------------------------------------------


def test_list_services_marks_a_flux_tag_on_stderr_alone_is_flagged(
    tmp_path, monkeypatch, capsys
):
    """stdout stays `deploy_tags.py list`'s alone: the locked half and deploy_ui parse it."""
    _, calls = run_front_half(monkeypatch, _repo(tmp_path), ["--list-services"])
    assert _deployed(calls)[0][-2:] == ["scripts/deploy_tools/deploy_tags.py", "list"]
    out = capsys.readouterr()
    assert out.out == ""
    assert "`bento` is applied by Flux" in out.err and "--tags bento exits 5" in out.err


def test_list_services_marks_nothing_without_an_owner_is_clean(
    tmp_path, monkeypatch, capsys
):
    repo = make_snapshot_repo(tmp_path / "repo")
    run_front_half(monkeypatch, repo, ["--list-services"])
    assert capsys.readouterr().err == ""
