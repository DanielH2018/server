"""GitOps Deploy — Status reads the `manual_plane` class of the `owed` ledger (#3392).

The reader ships before any writer records the class, because this pod redeploys on its own
schedule. Each line here is what a writer may put in the ledger, including keys this copy has
never heard of, and the page must read it exactly as it reads the line marker.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_gitops_owed.py
"""

import json
from dataclasses import replace

import checks.gitops

_LATE = 1000.0 + 7 * 3600


def _owed(**line) -> str:
    return json.dumps({"class": "manual_plane", "origin": "abc123def4567890", **line})


def test_a_ledger_line_with_an_unknown_key_pages_with_its_tags(cfg):
    """The Verify-by of #3392: a key a newer writer added is ignored, not a skipped line."""
    owed = _owed(
        subject="k3s",
        at=1000,
        playbook="ansible/k3s-bringup.yml",
        tags=["kubeconfig"],
        added_by_a_newer_writer={"any": "shape"},
    )
    ok, msg = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert not ok
    assert "apply `ansible/k3s-bringup.yml --tags kubeconfig` by hand" in msg
    assert msg.endswith("clear-manual-plane k3s --applied kubeconfig`")


def test_a_fresh_ledger_role_is_ok(cfg):
    """The age gate the line marker has: a role recorded ten minutes ago is a merge."""
    owed = _owed(subject="k3s", at=1000, playbook="ansible/k3s-bringup.yml", tags=[])
    ok, msg = checks.gitops.gitops_status(cfg, None, now=1000.0 + 600, owed=owed)
    assert ok
    assert msg == "no held deploy"


def test_a_ledger_line_missing_its_class_keys_still_pages_for_the_whole_role(cfg):
    """No `playbook` and a torn `tags` default safely; neither silences the page."""
    owed = _owed(subject="k3s", at=1000, tags="kubeconfig")
    ok, msg = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert not ok
    assert "apply the role by hand, then `" in msg
    assert msg.endswith("clear-manual-plane k3s`")


def test_a_whole_role_side_absorbs_a_narrowed_one_across_the_two_sources(cfg):
    """A role pending in both sources needs both changes applied.

    The line marker's row says the whole role, so a narrowed ledger line must not print an
    `--applied` clear that drops work nobody applied. The older stamp decides the age.
    """
    owed = _owed(
        subject="k3s", at=20000, playbook="ansible/k3s-bringup.yml", tags=["kubeconfig"]
    )
    ok, msg = checks.gitops.gitops_status(
        cfg,
        None,
        now=_LATE,
        manual_plane="abc123def4567890 ansible/k3s-bringup.yml k3s 1000.0",
        manual_plane_tags="k3s -",
        owed=owed,
    )
    assert not ok
    assert "unapplied for 7h" in msg
    assert msg.endswith("clear-manual-plane k3s`")


def test_two_narrowed_sides_union(cfg):
    owed = _owed(
        subject="k3s", at=1000, playbook="ansible/k3s-bringup.yml", tags=["registries"]
    )
    ok, msg = checks.gitops.gitops_status(
        cfg,
        None,
        now=_LATE,
        manual_plane="abc123def4567890 ansible/k3s-bringup.yml k3s 1000.0",
        manual_plane_tags="k3s kubeconfig",
        owed=owed,
    )
    assert not ok
    assert msg.endswith("clear-manual-plane k3s --applied kubeconfig,registries`")


def test_other_classes_in_the_ledger_page_nothing(cfg):
    """`k8s_unapplied` shares the file and pages on nothing, by construction (#2570)."""
    owed = json.dumps(
        {"class": "k8s_unapplied", "subject": "sonarr", "origin": "abc", "at": 1000}
    )
    ok, _ = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert ok


def test_check_gitops_status_reads_the_ledger_off_the_mount(tmp_path, cfg):
    """A torn byte on another line drops that line only, not the whole check (#2371)."""
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    torn = b'{"class": "k8s_unapplied", "subject": "son\xffarr"}'
    line = _owed(subject="k3s", at=1, playbook="ansible/k3s-bringup.yml", tags=[])
    (tmp_path / "owed.jsonl").write_bytes(torn + b"\n" + line.encode())
    ok, msg = checks.gitops.check_gitops_status(cfg)
    assert not ok
    assert msg.endswith("clear-manual-plane k3s`")
