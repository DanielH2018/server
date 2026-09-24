#!/usr/bin/env python3
"""The ranges the broad-tick suites drive `main()` with, and the paths that shape them.

Two test modules script the same tick: `test_gitops_deploy_broad_k8s.py` (the bumps a broad
range deploys) and `test_gitops_deploy_broad_deferrals.py` (what it says about the half it did
not). They share the checkout scaffolding rather than each building its own, so a change to
how a range is scripted cannot make the two disagree about what a broad tick sees.

Not a test module: `pytest` collects nothing here, and the `_` prefix is the same one
`_deploy_fakes.py` carries.
"""

import dataclasses

LOCAL = "1" * 40
ORIGIN = "2" * 40

K8S_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"
DECLARES_SONARR = "containers_list:\n  - name: sonarr\n    platform: k8s\n"

# A setup role NO playbook this deployer runs can apply: it fast-forwards and is recorded in
# `manual_plane`. The 2026-09-24 incident's range was this shape, nine bumps behind one such
# commit.
UNAPPLYABLE_ROLE = "ansible/roles/setup/k3s/tasks/main.yml"
# A deploy-plane path: `deploy_narrow.plan` gives the range an `ansible/deploy.yml` plan whose
# tag list `tick.narrow` scripts.
DEPLOY_PLANE = "ansible/inventory/group_vars/all.yml"
# A setup role the deployer DOES apply, so the broad plane runs a playbook of its own and the
# ordering between the two applies is observable.
APPLYABLE_ROLE = "ansible/roles/setup/gitops_deploy/tasks/main.yml"
# The SOPS-encrypted secrets file. It maps to no service template, so a rotation riding a
# broad range is named by the secrets channel or by nothing.
SECRETS = "ansible/vars/secrets.yml"
# A k8s role changed in a way that is NOT an image-pin bump, so it stays in `cs.k8s` and takes
# the defer-and-alert channel however the tick ends.
HAND_EDITED_K8S = "ansible/roles/k8s/radarr/tasks/main.yml"

DEPLOY_SONARR = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]
DEPLOY_PLANE_FULL = ["uv", "run", "--frozen", "ansible-playbook", "ansible/deploy.yml"]
APPLY_GITOPS_DEPLOY = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/initial_setup.yml",
    "--tags",
    "gitops_deploy",
]


def marker(state_dir, name: str) -> str | None:
    """One marker's stripped contents, or None when the file is not there."""
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


def mixed(settings, tick, *broad_paths, promote: bool = True):
    """A range carrying `broad_paths` beside a sonarr image-pin bump.

    Returns the `Config` to hand `main()`. Auto-deploy is armed on that object rather than by
    `monkeypatch`ing the entry module's globals: `main(tools, config)` takes both seams, and
    the `settings` fixture has already snapshotted the scripted checkout onto the config.

    Args:
        promote: whether k8s auto-deploy is armed. False leaves the bump in `cs.k8s`, which
            is the defer-and-alert path every rejecting half asserts against.
    """
    tick.declare(DECLARES_SONARR)
    tick.paths = [*broad_paths, K8S_DEFAULTS]
    tick.tree_listing = K8S_DEFAULTS + "\n"
    tick.files[f"{ORIGIN}:{K8S_DEFAULTS}"] = "sonarr_image: x:2\nk8s_autodeploy: true\n"
    tick.diffs["sonarr"] = "--- a\n+++ b\n-sonarr_image: x:1\n+sonarr_image: x:2\n"
    return dataclasses.replace(
        settings,
        k8s_autodeploy_enabled=promote,
        k8s_autodeploy_enabled_in_file=promote,
        k8s_autodeploy_denylist=frozenset(),
        k8s_autodeploy_pilot=frozenset(),
    )


def blocking(settings, tick, *broad_paths):
    """A mixed range whose staging consultation rejects, with blocking armed as on daniel-box."""
    config = mixed(settings, tick, *broad_paths)
    tick.staging_verdict = "rejected"
    return dataclasses.replace(config, staging_gate_blocking=True)


def plane_applies_radarr(settings, tick):
    """A range whose narrowed deploy plane applies a radarr bump, with sonarr's left to deploy.

    Returns the `Config` to hand `main()`. The two halves are what makes the annotation
    observable: radarr goes out with the plane, sonarr through the bump deploy under it.
    """
    config = mixed(settings, tick, DEPLOY_PLANE)
    radarr = "ansible/roles/k8s/radarr/defaults/main.yml"
    tick.declare(DECLARES_SONARR + "  - name: radarr\n    platform: k8s\n")
    tick.paths = [*tick.paths, radarr]
    tick.tree_listing += radarr + "\n"
    tick.files[f"{ORIGIN}:{radarr}"] = "radarr_image: x:2\nk8s_autodeploy: true\n"
    tick.diffs["radarr"] = "--- a\n+++ b\n-radarr_image: x:1\n+radarr_image: x:2\n"
    tick.narrow = (0, "radarr")
    return config
