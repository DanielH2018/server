"""Whether `land.sh`'s `needs-manual-apply` note narrows the tag, and what it falls back to.

The deferral for an unapplyable setup role is quoted by four surfaces: the deployer's journal
line, its Discord alert, the SessionStart banner and this note. Only the tick has the changed
paths in reach, so it derives the narrow tag once and records it in the `manual_plane_tags`
marker; every other surface reads that marker (#2307). This module pins the read, and the
fallback on every host where the marker is not there to read.

Run: uv run pytest scripts/deploy_tools/tests/test_land_quotes_the_deployers_narrowing.py
"""

import land_tags

# The range that provoked #2307: a three-line RBAC edit answered with `--tags k3s`, which
# restarts the control plane, where `--tags kubeconfig` was what it needed.
RBAC = ["ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2"]


def test_the_note_names_the_narrow_tag_the_deployer_recorded():
    """One derivation, four surfaces. The tick had the changed paths; this reads its answer.

    A second derivation here would be a second answer that can disagree with the journal line,
    the Discord alert and the SessionStart banner about the same deferral.
    """
    note = land_tags.plane_note(RBAC, narrow_tags={"k3s": frozenset({"kubeconfig"})})
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in note
    assert "--tags k3s`" not in note


def test_the_note_falls_back_to_the_role_tag_with_no_narrowing_in_reach():
    """The rejecting half, and the state of every host but the deployer.

    The marker lives in `/var/lib/gitops-deploy`, so a `plane_note` run anywhere else reads
    nothing and must print what it printed before the sidecar existed.
    """
    assert "ansible/k3s-bringup.yml --tags k3s" in land_tags.plane_note(RBAC)
