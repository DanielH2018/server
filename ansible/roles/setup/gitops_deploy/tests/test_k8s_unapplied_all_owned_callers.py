"""A shared role whose every caller is Flux-owned discharges its `k8s_unapplied` line (#5052).

`shared_role_callers.main` leaves a caller whose entry sets `owner` out of its caller set
(#4986), so a role only owned callers run comes back with no tag, the same as a role nothing
runs. Ansible has nothing left to apply for the first, so its line must drop; the second has no
proof at all, so its line must stay. The test drives the real `main` and the real
`deploy_narrow.shared_role_callers` decode, with only the `uv run` subprocess replaced, so it
holds whatever shape the fix gives the JSON between them.
`test_k8s_unapplied_owned_callers.py` has the mixed case, where some callers are owned.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_all_owned_callers.py
"""

import contextlib
import dataclasses
import io
import pathlib
import subprocess

import pytest

import shared_role_callers
from deployer_lib import deploy_k8s_owed, deploy_narrow
from deployer_lib.deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED
from lib.git_testing import commit, init_repo

ORIGIN = "a" * 40
# Tag names absent from the real tree, so a fix that reads the real host_vars stays red.
OWNED_CALLERS = ("pdf-flux-a", "pdf-flux-b")
ANSIBLE_CALLER = "web-plain"
# A role with tasks that no role includes, so no tag runs it.
ORPHAN = "orphan-lib"


def _all_owned_checkout(root: pathlib.Path, role: str) -> None:
    """A checkout where `role` is included only by two `owner: flux` entries.

    `web-plain` is an Ansible-owned entry that includes no shared role, so the tree declares a
    tag Ansible deploys without it reaching `role`. `orphan-lib` is included by nothing.
    """
    entries = f"  - name: {ANSIBLE_CALLER}\n    platform: k8s\n" + "".join(
        f"  - name: {tag}\n    platform: k8s\n    owner: flux\n"
        for tag in OWNED_CALLERS
    )
    task = "- ansible.builtin.debug:\n    msg: render\n"
    files = {
        "ansible/inventory/host_vars/daniel-box.yml": "containers_list:\n" + entries,
        f"ansible/roles/k8s/{role}/tasks/main.yml": task,
        f"ansible/roles/k8s/{ORPHAN}/tasks/main.yml": task,
        f"ansible/roles/k8s/{ANSIBLE_CALLER}/tasks/main.yml": task,
    }
    for tag in OWNED_CALLERS:
        files[f"ansible/roles/k8s/{tag}/tasks/main.yml"] = (
            f"- ansible.builtin.include_role:\n    name: k8s/{role}\n"
        )
    init_repo(root)
    commit(root, "a shared role only owned entries call", **files)


def _main_in_process(monkeypatch) -> None:
    """Run the deployer's `uv run ... shared_role_callers.py` call in-process.

    Only the transport is replaced: every argument after the script path reaches `main`, so a
    flag the fix adds still arrives, and every other `subprocess.run` — `main`'s own git reads
    among them — goes to the real one.
    """
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        argv = list(argv)
        if deploy_narrow.SHARED_CALLERS_SCRIPT not in argv:
            return real_run(argv, *args, **kwargs)
        rest = argv[argv.index(deploy_narrow.SHARED_CALLERS_SCRIPT) + 1 :]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = shared_role_callers.main([*rest, "--repo", str(kwargs["cwd"])])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return subprocess.CompletedProcess(argv, code, out.getvalue(), "")

    monkeypatch.setattr(subprocess, "run", run)


# `manifests` discharges on a render digest; `stats-lib` stands for `image-builder` and the
# other shared roles that do not, so a fix confined to the digest path stays red.
@pytest.mark.parametrize("role", ["manifests", "stats-lib"])
def test_a_role_only_owned_callers_run_drops_while_a_role_nothing_runs_keeps(
    gitops_deploy, settings, state, tmp_path, monkeypatch, role
):
    """FLAGGED half: every caller of `role` is `owner: flux`, so its line drops.

    The kept half rides along because it is green on its own: `orphan-lib` names no tag either,
    and a fix that read every empty caller set as "drop" would wrongly discharge it.
    """
    root = tmp_path / "all-owned"
    _all_owned_checkout(root, role)
    config = dataclasses.replace(settings, repo=str(root))
    _main_in_process(monkeypatch)
    tools = DeployTools(
        discord_post=lambda _webhook, _content: True,
        release_commit=lambda _svc: None,
        is_ancestor=lambda _repo, _origin, _sha: True,
        render_proof=lambda _svc: None,
        digest_provable=lambda _repo, _roles: set(),
        digest_covers=lambda _repo, _items: set(),
    )

    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {role, ORPHAN}, 1000.0)
    assert deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config) == [role]
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == [ORPHAN]
