"""A shared role's `k8s_unapplied` line does not wait on a caller Ansible never deploys (#4986).

A caller whose `containers_list` entry sets `owner` gets no release record, because `deploy.sh`
refuses it, and no render record, because `render_targets.render_record_targets` leaves it out.
Waited on, it keeps the line forever. The test below drives the real
`shared_role_callers.main` against a throwaway checkout rather than a hand-written caller set,
so it holds whether the owned callers are dropped there or on the deployer side that reads it.
`test_k8s_unapplied_marker.py` has the rest of the discharge.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_owned_callers.py
"""

import contextlib
import dataclasses
import io
import json
import pathlib

import pytest

import shared_role_callers
from deployer_lib import deploy_k8s_owed
from deployer_lib.deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED
from lib.git_testing import commit, init_repo

ORIGIN = "a" * 40
APPLIED = "b" * 40
LATER = "c" * 40
# Tag names absent from the real tree, so a fix that reads the real host_vars stays red.
OWNED_CALLERS = {"pdf-flux": "flux", "pdf-released": "released"}
ANSIBLE_CALLERS = ("web-a", "web-b")
# `manifests` discharges on a render digest; `stats-lib` stands for `image-builder` and the
# other shared roles that do not, so a fix confined to the digest path stays red.
SHARED_ROLES = ("manifests", "stats-lib")


def _owned_caller_checkout(root: pathlib.Path) -> None:
    """A checkout where four roles include both shared roles and two of their entries set `owner`.

    `shared_role_callers.main` reads the declared tags at HEAD through git and walks the role
    graph from the working tree, so both halves are committed.
    """
    entries = [f"  - name: {tag}\n    platform: k8s\n" for tag in ANSIBLE_CALLERS] + [
        f"  - name: {tag}\n    platform: k8s\n    owner: {owner}\n"
        for tag, owner in OWNED_CALLERS.items()
    ]
    files = {
        "ansible/inventory/host_vars/daniel-box.yml": "containers_list:\n"
        + "".join(entries),
    }
    for role in SHARED_ROLES:
        files[f"ansible/roles/k8s/{role}/tasks/main.yml"] = (
            "- ansible.builtin.debug:\n    msg: render\n"
        )
    for tag in (*ANSIBLE_CALLERS, *OWNED_CALLERS):
        files[f"ansible/roles/k8s/{tag}/tasks/main.yml"] = "".join(
            f"- ansible.builtin.include_role:\n    name: k8s/{role}\n"
            for role in SHARED_ROLES
        )
    init_repo(root)
    commit(root, "four callers of both shared roles", **files)


def _callers_from_main(repo, roles) -> dict[str, set[str]]:
    """`deploy_narrow.shared_role_callers`'s decode of `main`, run in-process, not over uv."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert shared_role_callers.main([*sorted(roles), "--repo", repo]) == 0
    return {role: set(tags) for role, tags in json.loads(out.getvalue()).items()}


@pytest.mark.parametrize("role", SHARED_ROLES)
def test_a_shared_role_line_discharges_once_every_ansible_owned_caller_carries_it(
    gitops_deploy, settings, state, tmp_path, role
):
    """FLAGGED half: both Ansible-owned callers carry the change, so the line drops.

    The kept half follows: with `web-b` behind, the owned callers' missing records must not
    stand in for its proof.
    """
    root = tmp_path / "owned-callers"
    _owned_caller_checkout(root)
    config = dataclasses.replace(settings, repo=str(root))

    def discharge(behind):
        tools = DeployTools(
            discord_post=lambda _webhook, _content: True,
            release_commit=lambda svc: (
                APPLIED + svc if svc in ANSIBLE_CALLERS else None
            ),
            is_ancestor=lambda _repo, _origin, sha: sha[40:] not in behind,
            shared_role_callers=_callers_from_main,
            render_proof=lambda _svc: None,
            digest_provable=lambda _repo, _roles: set(),
            digest_covers=lambda _repo, _items: set(),
        )
        return deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config)

    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {role}, 1000.0)
    assert discharge(behind=set()) == [role]
    assert state.owed_pending(OWED_K8S_UNAPPLIED) == []

    state.record_owed(OWED_K8S_UNAPPLIED, LATER, {role}, 2000.0)
    assert discharge(behind={"web-b"}) == []
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == [role]
