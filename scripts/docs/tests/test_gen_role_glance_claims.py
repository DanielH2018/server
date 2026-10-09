"""Tests for the Claims line of a k8s role's "At a glance" block: each claim and its tier.

Split from test_gen_role_glance.py for the module-length cap. The tier comes from
`catalog_backup.claim_tiers` over the k3s role's Longhorn tier lists, so each test moves a
claim between lists and checks the line moves with it.
"""

import yaml

import gen_role_glance as g


def _claims_line(entry, role_dir, roles, k3s_defaults, group_vars=None):
    lines = g.glance_lines(
        entry,
        role_dir,
        group_vars=group_vars or {},
        k8s_roles=roles,
        k3s_defaults=k3s_defaults,
    )
    return next(line for line in lines if line.startswith("- **Claim"))


def test_claims_line_carries_each_claims_tier_and_moves_with_the_tier_lists(tmp_path):
    """A tier beside every named claim, read from the lists — never typed (red-proof pair)."""
    roles = tmp_path / "roles"
    entry = {"name": "svc", "platform": "k8s"}
    role = roles / "svc"
    (role / "templates").mkdir(parents=True)
    (role / "tasks").mkdir()
    # The three declaration shapes: inline PVC, a volume-claim include, a `claimName:`
    # reference to a claim another role declares off Longhorn.
    (role / "templates" / "pvc.yaml.j2").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: svc-cache\n"
        "spec:\n  storageClassName: longhorn-nobackup\n"
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "      volumes:\n"
        "        - persistentVolumeClaim:\n            claimName: svc-config\n"
        "        - persistentVolumeClaim:\n            claimName: media-data\n"
    )
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n    name: k8s/volume-claim\n"
        "  vars:\n    volume_claim_name: svc-config\n"
        "    volume_claim_storage_class: longhorn\n"
    )
    (roles / "media-volume" / "templates").mkdir(parents=True)
    (roles / "media-volume" / "templates" / "pvc.yaml.j2").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: media-data\n"
        "spec:\n  storageClassName: media-local\n"
    )
    k3s_defaults = tmp_path / "k3s.yml"
    k3s_defaults.write_text(
        yaml.safe_dump({"k3s_longhorn_weekly_volumes": ["homelab/svc-config"]})
    )
    assert _claims_line(entry, role, roles, k3s_defaults) == (
        "- **Claims:** `svc-config` (weekly -> B2 (default target)), "
        "`media-data` (not Longhorn (media-local)), "
        "`svc-cache` (no backup (StorageClass longhorn-nobackup))"
    )
    # Moving the claim between lists moves the line, so the gate catches an edited list.
    k3s_defaults.write_text(
        yaml.safe_dump({"k3s_longhorn_nobackup_volumes": ["homelab/svc-config"]})
    )
    assert "`svc-config` (no backup (listed in k3s_longhorn_nobackup_volumes))" in (
        _claims_line(entry, role, roles, k3s_defaults)
    )


def test_the_committed_blocks_name_a_tier_from_each_list():
    """Non-vacuity: a resolver that stopped matching would print `unknown` everywhere and pass."""
    # `render_block` wraps a long Claims line, so read each doc with its whitespace folded.
    sonarr = " ".join((g.K8S_ROLES / "sonarr" / "CLAUDE.md").read_text().split())
    kuma = " ".join((g.K8S_ROLES / "uptime-kuma" / "CLAUDE.md").read_text().split())
    assert "`sonarr-config` (weekly -> B2 (default target))" in sonarr
    assert "`media-data` (not Longhorn (media-local))" in sonarr
    assert (
        "`uptime-kuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes))"
        in kuma
    )


def test_claims_line_reads_a_group_vars_override_of_a_tier_list(tmp_path):
    """An all.yml override of a tier list moves the claim's tier, as in a deploy (#3918)."""
    roles = tmp_path / "roles"
    role = roles / "svc"
    (role / "templates").mkdir(parents=True)
    (role / "templates" / "pvc.yaml.j2").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: svc-config\n"
        "spec:\n  storageClassName: longhorn\n"
    )
    k3s_defaults = tmp_path / "k3s.yml"
    k3s_defaults.write_text(
        yaml.safe_dump(
            {
                "k3s_longhorn_r2_volumes": [],
                "k3s_longhorn_weekly_volumes": ["homelab/svc-config"],
            }
        )
    )
    entry = {"name": "svc", "platform": "k8s"}
    assert "`svc-config` (weekly" in _claims_line(entry, role, roles, k3s_defaults)
    override = {
        "k3s_longhorn_weekly_volumes": [],
        "k3s_longhorn_nobackup_volumes": ["homelab/svc-config"],
    }
    assert "`svc-config` (no backup (listed in k3s_longhorn_nobackup_volumes))" in (
        _claims_line(entry, role, roles, k3s_defaults, group_vars=override)
    )
