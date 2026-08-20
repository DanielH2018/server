"""Derive the gitops auto-deploy denylist from each k8s role's own declaration.

Every role under roles/k8s/ declares `k8s_autodeploy` (plus a `k8s_autodeploy_reason`
giving the mechanism) in its defaults/main.yml. This filter collects the ones declaring
false; gitops_deploy renders the result into K8S_AUTODEPLOY_DENYLIST, which
deploy_logic.split_k8s_auto_deploy consults before promoting an image bump to an
unattended deploy.

The direction of failure is the whole design. A role absent from the returned list is
auto-deployable, so anything this filter cannot read with certainty must raise rather
than quietly drop out. That is the defect the CSV this replaces was prone to: absence
meant eligible, and two roles (seed-volume, image-builder) were eligible for months
purely because nobody had typed them into the list.
"""

from __future__ import annotations

import os

import yaml
from ansible.errors import AnsibleFilterError

DECLARATION = "k8s_autodeploy"
REASON = "k8s_autodeploy_reason"

# Roles under roles/k8s/ that deploy no service of their own — they are *included* by other
# roles (manifests renders and applies; rollout-drain waits on the rollout). They carry no
# defaults/main.yml and declare no stance. Every other role must declare one: a missing
# declaration is an error, never an implicit "eligible".
#
# The invariant that makes this list safe: no role named here may pin an `_image:` var,
# because pinning one is what makes a role Renovate-visible and therefore auto-deployable
# at all. seed-volume pins seed_volume_image and so does NOT belong here — it declares
# false like any other role.
SHARED_ROLES = frozenset({"manifests", "rollout-drain"})


def _check_shared_roles(roles_dir):
    """Enforce the SHARED_ROLES invariant instead of just documenting it.

    A typo'd or stale member would otherwise exclude nothing (silently — sorted() over
    os.listdir() never notices a name that isn't there) and no test would catch it, which
    is exactly the class of gap that left seed-volume and image-builder auto-deployable
    for months under the CSV this replaces. This checks defaults/main.yml only, because
    that is where this repo's Renovate manager reads image pins from.
    """
    for member in sorted(SHARED_ROLES):
        member_dir = os.path.join(roles_dir, member)
        if not os.path.isdir(member_dir):
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: SHARED_ROLES member '{member}' is not a "
                f"directory under {roles_dir}. A stale or typo'd entry here excludes "
                f"nothing, which would silently make it a candidate for the denylist "
                f"instead. Fix the name in SHARED_ROLES or remove the entry."
            )

        defaults_path = os.path.join(member_dir, "defaults", "main.yml")
        if not os.path.isfile(defaults_path):
            continue

        try:
            with open(defaults_path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: cannot read SHARED_ROLES member '{member}' "
                f"defaults ({defaults_path}): {exc}"
            ) from exc

        if isinstance(data, dict):
            image_keys = [key for key in data if str(key).endswith("_image")]
            if image_keys:
                raise AnsibleFilterError(
                    f"k8s_autodeploy_denylist: SHARED_ROLES member '{member}' pins "
                    f"{image_keys} in {defaults_path}. Pinning an `_image:` var is what "
                    f"makes a role Renovate-visible and therefore auto-deployable, so a "
                    f"shared role that pins one must not be silently skipped — remove it "
                    f"from SHARED_ROLES and let it declare k8s_autodeploy like any other "
                    f"role."
                )


def k8s_autodeploy_denylist(playbook_dir):
    """Role names that must never be auto-deployed, sorted.

    `playbook_dir` is Ansible's magic var — the directory holding deploy.yml, i.e. the
    repo's `ansible/` directory.
    """
    roles_dir = os.path.join(playbook_dir, "roles", "k8s")
    if not os.path.isdir(roles_dir):
        raise AnsibleFilterError(
            f"k8s_autodeploy_denylist: no such directory: {roles_dir}"
        )

    _check_shared_roles(roles_dir)

    denied = []
    for role in sorted(os.listdir(roles_dir)):
        entry_path = os.path.join(roles_dir, role)
        if not os.path.isdir(entry_path):
            if role.startswith("."):
                continue
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: '{entry_path}' is not a directory (possibly a "
                f"dangling symlink). Every entry under roles/k8s/ must be a real role "
                f"directory declaring `{DECLARATION}`; remove it or fix the symlink rather "
                f"than let it silently drop out of the denylist."
            )
        if role in SHARED_ROLES:
            continue

        path = os.path.join(roles_dir, role, "defaults", "main.yml")
        if not os.path.isfile(path):
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: role '{role}' has no defaults/main.yml and so "
                f"declares no `{DECLARATION}` stance. Add one (copy the shape from a "
                f"sibling role), or add the role to SHARED_ROLES if it deploys no service "
                f"of its own. Refusing to treat a silent role as auto-deployable."
            )

        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: cannot read '{role}' defaults ({path}): {exc}"
            ) from exc

        if not isinstance(data, dict) or DECLARATION not in data:
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: role '{role}' does not set `{DECLARATION}` in "
                f"{path}. Every role under roles/k8s/ must declare whether it is safe to "
                f"deploy unattended."
            )

        value = data[DECLARATION]
        if not isinstance(value, bool):
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: role '{role}' sets `{DECLARATION}: {value!r}`, "
                f"which is not a boolean. Use true or false — a quoted string would make "
                f"every role truthy."
            )

        reason = data.get(REASON)
        if not isinstance(reason, str) or not reason.strip():
            raise AnsibleFilterError(
                f"k8s_autodeploy_denylist: role '{role}' sets `{DECLARATION}` but no "
                f"`{REASON}`. The reason is what makes the stance reviewable; a bare "
                f"boolean cannot be audited."
            )

        if value is False:
            denied.append(role)

    if not denied:
        raise AnsibleFilterError(
            "k8s_autodeploy_denylist: derived an EMPTY denylist from "
            f"{roles_dir}. gitops_deploy reads an empty denylist as 'feature disabled', so "
            "this would disarm auto-deploy rather than widen it — but an empty result still "
            "means the derivation is broken, so fail rather than render it."
        )

    return denied


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"k8s_autodeploy_denylist": k8s_autodeploy_denylist}
