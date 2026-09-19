#!/usr/bin/env python3
"""Tests for the Longhorn backup-tier half of scripts/docs/catalog_backup.py.

Fixture-driven like test_service_catalog.py (the synthetic repo is `_catalog_fixtures`),
with one deliberate exception at the end that reads the real roles. Split out of
test_service_catalog.py when the StorageClass axis (#2095) took it past the module-length cap.
Run: uv run pytest scripts/docs/tests/test_catalog_backup.py
"""

import service_catalog
from _catalog_fixtures import make_repo, write
from catalog_backup import LonghornTiers, backup_tier, claim_index
from catalog_model import K8S_ROLES


def test_backup_tier_literal_pvc_name_in_r2_list(tmp_path):
    paths = make_repo(tmp_path)
    role = paths["k8s_roles"] / "authelia" / "templates"
    write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: authelia-config
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "authelia")
    assert row.backup_tier == "daily -> R2"


def test_backup_tier_literal_pvc_name_in_weekly_list(tmp_path):
    paths = make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: jellyfin-config
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_longhorn_class_in_no_list_is_the_default_group(tmp_path):
    paths = make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: some-other-claim
        spec:
          storageClassName: longhorn
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "daily -> B2 (default group)"


def test_backup_tier_no_pvc_is_stateless_not_unknown(tmp_path):
    paths = make_repo(tmp_path)
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "authelia")
    assert row.backup_tier == "no PVC (stateless)"
    entry = {"name": "authelia"}
    tier = backup_tier(entry, "k8s", "homelab", LonghornTiers(), paths["k8s_roles"])
    assert tier == row.backup_tier


def test_backup_tier_unresolvable_var_is_unknown(tmp_path):
    paths = make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: {{ jellyfin_k8s_claim }}
        """,
    )
    # No defaults/main.yml giving jellyfin_k8s_claim a literal value.
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier.startswith("unknown")


def test_backup_tier_var_resolved_from_role_defaults(tmp_path):
    paths = make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role_templates / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: {{ jellyfin_k8s_claim }}
        """,
    )
    write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "jellyfin_k8s_claim: jellyfin-config\n",
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_finds_pvc_referenced_only_by_claimname(tmp_path):
    # home-assistant's real shape: no pvc.yaml.j2 of its own, just a claimName: reference
    # to a PVC provisioned elsewhere in a deployment/pod template.
    paths = make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role_templates / "deployment.yaml.j2",
        """\
        volumes:
          - name: config
            persistentVolumeClaim:
              claimName: jellyfin-config
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_docker_is_not_longhorn(tmp_path):
    paths = make_repo(tmp_path)
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "dozzle")
    assert row.backup_tier == "n/a (Docker/Pi, not Longhorn-backed)"


def test_multiple_pvcs_report_each_tier_deduplicated(tmp_path):
    paths = make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    write(
        role_templates / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: jellyfin-config
        ---
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: authelia-config
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target); daily -> R2"


# --- Backup tier: the StorageClass axis (#2095) ---------------------------------------------
#
# Longhorn backs a volume up only when its StorageClass asks (`longhorn`, not
# `longhorn-nobackup`) AND the k3s role's no-backup list leaves it alone. One accept/reject
# pair per branch of `_classify`, then one test per shape a role declares a claim in — the
# class is found by pattern, so a regex that stops matching would otherwise turn every claim
# of that shape "unknown" without any test noticing.


def _pvc_block(name, storage_class=None):
    text = f"apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: {name}\n"
    if storage_class is not None:
        text += f"spec:\n  storageClassName: {storage_class}\n"
    return text


def _jellyfin_tier(paths, template_text):
    write(paths["k8s_roles"] / "jellyfin" / "templates" / "pvc.yaml.j2", template_text)
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    return row.backup_tier


def test_backup_tier_class_outside_longhorn_is_not_longhorn(tmp_path):
    # media-volume's shape: a local-path class Longhorn never sees, listed nowhere.
    paths = make_repo(tmp_path)
    assert _jellyfin_tier(paths, _pvc_block("media-data", "media-local")) == (
        "not Longhorn (media-local)"
    )


def test_backup_tier_longhorn_class_is_not_reported_as_not_longhorn(tmp_path):
    paths = make_repo(tmp_path)
    tier = _jellyfin_tier(paths, _pvc_block("some-other-claim", "longhorn"))
    assert not tier.startswith("not Longhorn")


def test_backup_tier_nobackup_class_is_no_backup(tmp_path):
    # karakeep-meili's shape: excluded by class alone, in no list.
    paths = make_repo(tmp_path)
    assert _jellyfin_tier(paths, _pvc_block("karakeep-meili", "longhorn-nobackup")) == (
        "no backup (StorageClass longhorn-nobackup)"
    )


def test_backup_tier_backup_class_off_every_list_is_not_no_backup(tmp_path):
    paths = make_repo(tmp_path)
    tier = _jellyfin_tier(paths, _pvc_block("some-other-claim", "longhorn"))
    assert not tier.startswith("no backup")


def test_backup_tier_nobackup_list_overrides_a_backup_class(tmp_path):
    # uptime-kuma's shape: `longhorn` by class, excluded by the list — the list wins, as
    # longhorn.yml's reconciliation applies it.
    paths = make_repo(tmp_path)
    assert _jellyfin_tier(paths, _pvc_block("uptime-kuma-data", "longhorn")) == (
        "no backup (listed in k3s_longhorn_nobackup_volumes)"
    )


def test_backup_tier_nobackup_list_beats_the_weekly_list(tmp_path):
    paths = make_repo(tmp_path)
    write(
        paths["k3s_defaults"],
        """\
        k3s_longhorn_r2_volumes: []
        k3s_longhorn_weekly_volumes:
          - homelab/uptime-kuma-data
        k3s_longhorn_nobackup_volumes:
          - homelab/uptime-kuma-data
        """,
    )
    assert _jellyfin_tier(paths, _pvc_block("uptime-kuma-data", "longhorn")).startswith(
        "no backup"
    )


def test_backup_tier_unresolvable_class_off_every_list_is_unknown_not_default(tmp_path):
    # The bug's shape: before #2095 a claim with no readable class fell through to
    # "daily -> B2 (default group)", which is a guess, not a derivation.
    paths = make_repo(tmp_path)
    tier = _jellyfin_tier(paths, _pvc_block("some-other-claim"))
    assert tier.startswith("unknown")
    assert "StorageClass" in tier


def test_backup_tier_unresolvable_class_on_a_list_is_classified_by_the_list(tmp_path):
    # The lists are Longhorn's own selectors, so membership settles the tier on its own.
    paths = make_repo(tmp_path)
    assert _jellyfin_tier(paths, _pvc_block("jellyfin-config")) == (
        "weekly -> B2 (default target)"
    )


def test_backup_tier_reads_class_from_the_pvc_macro_call(tmp_path):
    # Shape 2: `pvc(name, storage_class, size)` — a bare variable resolves through the role's
    # defaults, a quoted literal stands as written.
    paths = make_repo(tmp_path)
    write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "jellyfin_k8s_claim: jellyfin-cache\njellyfin_k8s_storage_class: longhorn-nobackup\n",
    )
    template = (
        "{% from 'pvc.yml.j2' import pvc with context %}\n"
        "{{ pvc(jellyfin_k8s_claim, jellyfin_k8s_storage_class, jellyfin_k8s_size) }}\n"
        "---\n"
        "{% call pvc('jellyfin-config', 'longhorn', jellyfin_k8s_size) %}\n"
        "  # why this one is backed up\n"
        "{% endcall %}\n"
    )
    assert _jellyfin_tier(paths, template) == (
        "no backup (StorageClass longhorn-nobackup); weekly -> B2 (default target)"
    )


def test_backup_tier_reads_class_from_a_volume_claim_include(tmp_path):
    # Shape 3: the claim is rendered by the volume-claim role on the caller's behalf, so
    # nothing in the caller's templates/ names its class — only the include task's vars do.
    paths = make_repo(tmp_path)
    write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "jellyfin_k8s_claim: jellyfin-cache\njellyfin_k8s_storage_class: longhorn-nobackup\n",
    )
    write(
        paths["k8s_roles"] / "jellyfin" / "tasks" / "main.yml",
        """\
        - name: Provision the cache claim
          ansible.builtin.include_role:
            name: k8s/volume-claim
          vars:
            volume_claim_name: "{{ jellyfin_k8s_claim }}"
            volume_claim_storage_class: "{{ jellyfin_k8s_storage_class }}"
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "no backup (StorageClass longhorn-nobackup)"


def test_backup_tier_volume_claim_include_without_a_class_reads_the_role_default(
    tmp_path,
):
    # terraria-stats' shape: no volume_claim_storage_class on the include, so the class is
    # the volume-claim role's own default — read from that role, not assumed.
    paths = make_repo(tmp_path)
    write(
        paths["k8s_roles"] / "volume-claim" / "defaults" / "main.yml",
        "volume_claim_storage_class: longhorn\n",
    )
    write(
        paths["k8s_roles"] / "jellyfin" / "tasks" / "main.yml",
        """\
        - name: Provision the claim
          ansible.builtin.include_role:
            name: k8s/volume-claim
          vars:
            volume_claim_name: some-other-claim
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "daily -> B2 (default group)"


def test_backup_tier_volume_claim_include_with_no_default_anywhere_is_unknown(tmp_path):
    paths = make_repo(tmp_path)
    write(
        paths["k8s_roles"] / "jellyfin" / "tasks" / "main.yml",
        """\
        - name: Provision the claim
          ansible.builtin.include_role:
            name: k8s/volume-claim
          vars:
            volume_claim_name: some-other-claim
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier.startswith("unknown")


def test_backup_tier_claimname_reference_finds_the_class_in_the_declaring_role(
    tmp_path,
):
    # media-data's shape: declared (with its class) by media-volume, mounted by seven other
    # roles that name it only through `claimName:`.
    paths = make_repo(tmp_path)
    write(
        paths["k8s_roles"] / "media-volume" / "templates" / "pv.yaml.j2",
        _pvc_block("media-data", "media-local"),
    )
    write(
        paths["k8s_roles"] / "jellyfin" / "templates" / "deployment.yaml.j2",
        """\
        volumes:
          - name: media
            persistentVolumeClaim:
              claimName: media-data
        """,
    )
    row = next(r for r in service_catalog.build_rows(**paths) if r.name == "jellyfin")
    assert row.backup_tier == "not Longhorn (media-local)"


def test_claim_index_reads_the_real_tree_for_each_declaration_shape():
    """The one test here that reads the real roles, on purpose.

    Every fixture above hands the parser a shape it already matches. CLAUDE.md's rule for a
    check that finds its subject by pattern is a named member it must find: one claim per
    declaration shape (inline block, `pvc()` macro call, volume-claim include), so a regex
    or task walk that stops matching names the claim it lost rather than moving a count.
    """
    index = claim_index(K8S_ROLES)
    expected = {
        "media-data": "media-local",  # inline block, media-volume
        "karakeep-meili": "longhorn-nobackup",  # pvc() macro call, karakeep
        "uptime-kuma-data": "longhorn",  # volume-claim include, uptime-kuma
    }
    missing = {name: sc for name, sc in expected.items() if index.get(name) != sc}
    assert not missing, f"claim_index no longer resolves: {missing}"
