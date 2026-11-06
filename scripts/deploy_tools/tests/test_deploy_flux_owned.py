"""`deploy.sh` refuses a k8s tag, leaves one out of `--changed`, and marks each in a listing.

Flux applies every k8s entry through `promote`, so no Ansible path may apply one
(docs/target-architecture/deploy/d6-delete-old-path.md, *`land.sh`'s k8s tags after D6*). These
cases run the front half against a throwaway repo whose `containers_list` declares one such
entry, `bento`, beside the non-k8s ones `make_snapshot_repo` writes.

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
def test_a_typed_k8s_tag_is_flagged(owner, tmp_path, monkeypatch, capsys):
    """Exit 5 for the whole list, before the locked half takes any lock."""
    code, calls = run_front_half(
        monkeypatch, _repo(tmp_path, owner), ["--tags", "alpha,bento"]
    )
    assert code == DEPLOY_FLUX_OWNED
    assert _deployed(calls) == []
    err = capsys.readouterr().err
    assert "`bento` is applied by Flux through `promote`" in err
    assert "No Ansible path may apply `bento`" in err


def test_a_typed_k8s_tag_with_no_owner_is_flagged(tmp_path, monkeypatch):
    """The refusal keys on the platform, so it does not lean on the `owner` flag."""
    repo = make_snapshot_repo(tmp_path / "repo")
    host = repo / HOST_FILE
    host.write_text(host.read_text() + "  - { name: bento, platform: k8s }\n")
    code, calls = run_front_half(monkeypatch, repo, ["--tags", "bento"])
    assert code == DEPLOY_FLUX_OWNED
    assert _deployed(calls) == []


def test_a_typed_tag_with_no_k8s_entry_is_clean(tmp_path, monkeypatch):
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


def test_at_reads_the_k8s_entries_at_the_commit_it_deploys_is_flagged(
    tmp_path, monkeypatch
):
    """An `--at` commit declares the k8s entry before the checkout has it."""
    repo = _repo(tmp_path)
    git(repo, "commit", "-qam", "declare bento", "--no-gpg-sign")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    host = repo / HOST_FILE
    host.write_text(host.read_text() + "  - { name: bento, platform: docker }\n")

    assert run_front_half(monkeypatch, repo, ["--tags", "bento"])[0] is None
    code, _ = run_front_half(monkeypatch, repo, ["--at", sha, "--tags", "bento"])
    assert code == DEPLOY_FLUX_OWNED


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


def test_landings_changed_call_leaves_a_k8s_tag_out_is_flagged(tmp_path, capsys):
    """land.sh's `deploy_tags.py changed` derives no k8s tag either."""
    status = deploy_tags.changed(
        "origin/master", cwd=_repo(tmp_path), git_diff=_diff(["bento"])
    )
    assert status == 0
    assert capsys.readouterr().out == ""


def test_changed_keeps_the_non_k8s_tags_beside_it_is_clean(tmp_path, monkeypatch):
    code, calls = _changed(monkeypatch, _repo(tmp_path), ["bento", "alpha"])
    assert code is None
    assert _deployed(calls) == [["in-process", "alpha"]]


# -- deploy.sh --tags manifests ------------------------------------------------------------


def test_a_typed_shared_role_reaches_the_tag_validation_as_typed_is_flagged(
    tmp_path, monkeypatch
):
    """Nothing expands it to its callers, so `validate` refuses the name with exit 2."""
    repo = _repo(tmp_path, helper_callers=("alpha", "beta"))
    _, calls = run_front_half(monkeypatch, repo, ["--tags", "helper", "--check"])
    assert [call[1] for call in calls if call[0] == "validate"] == [("helper",)]


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


def test_list_services_marks_nothing_without_a_k8s_entry_is_clean(
    tmp_path, monkeypatch, capsys
):
    repo = make_snapshot_repo(tmp_path / "repo")
    run_front_half(monkeypatch, repo, ["--list-services"])
    assert capsys.readouterr().err == ""
