"""GitOps Deploy — Status reads the `manual_plane`, `k8s_deferred` and `hold_plane` classes of `owed`.

The ledger replaces the line markers under #3392.

This pod redeploys on its own schedule, so a writer can be newer than this copy. Each line
here is what a writer may put in the ledger, including keys this copy has never heard of, and
the page must still read it.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_gitops_owed.py
"""

import json
from dataclasses import replace

import checks.gitops
from _fake_sources import FakeSources
from bridge.common import cap_push_msg
from gitops_markers import HOLD_CLEAR_CMD

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
    assert msg.endswith("clear-owed manual_plane k3s --applied kubeconfig`")


def test_a_fresh_ledger_role_is_ok(cfg):
    """A role recorded ten minutes ago is an ordinary merge, not a fault."""
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
    assert msg.endswith("clear-owed manual_plane k3s`")


def test_other_classes_in_the_ledger_page_nothing(cfg):
    """`k8s_unapplied` shares the file and pages on nothing, by construction (#2570)."""
    owed = json.dumps(
        {"class": "k8s_unapplied", "subject": "sonarr", "origin": "abc", "at": 1000}
    )
    ok, _ = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert ok


def test_a_k8s_deferred_ledger_line_with_an_unknown_key_pages(cfg):
    """The Verify-by of #3392 for `k8s_deferred`: a key a newer writer added still pages."""
    owed = json.dumps(
        {
            "class": "k8s_deferred",
            "subject": "sonarr",
            "origin": "abc123def4567890",
            "at": 1000,
            "added_by_a_newer_writer": 1,
        }
    )
    ok, msg = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert not ok
    assert msg.startswith("sonarr merged but not deployed for 7h")
    assert msg.endswith("clear-owed k8s_deferred sonarr`")


def test_a_k8s_deferred_bump_on_two_lines_dates_from_the_older(cfg):
    """A service two ledger lines both hold is one bump, aged from its first."""
    owed = "\n".join(
        json.dumps(
            {"class": "k8s_deferred", "subject": "sonarr", "origin": sha, "at": at}
        )
        for sha, at in (("a" * 40, _LATE - 600), ("b" * 40, 1000))
    )
    ok, msg = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert not ok
    assert msg.startswith("sonarr merged but not deployed for 7h")


def test_check_gitops_status_reads_the_ledger_off_the_mount(tmp_path, cfg):
    """A torn byte on another line drops that line only, not the whole check (#2371)."""
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    torn = b'{"class": "k8s_unapplied", "subject": "son\xffarr"}'
    line = _owed(subject="k3s", at=1, playbook="ansible/k3s-bringup.yml", tags=[])
    (tmp_path / "owed.jsonl").write_bytes(torn + b"\n" + line.encode())
    ok, msg = checks.gitops.check_gitops_status(cfg, FakeSources())
    assert not ok
    assert msg.endswith("clear-owed manual_plane k3s`")


def _held(subject: str, at: int, **extra) -> str:
    return json.dumps(
        {"class": "hold_plane", "subject": subject, "origin": "abc", "at": at, **extra}
    )


def test_a_hold_plane_ledger_line_with_an_unknown_key_is_read_oldest_first(cfg):
    """The Verify-by of #3392 for `hold_plane`: a newer writer's extra key skips no line.

    A plane on two lines is one plane, so the count stays honest.
    """
    owed = "\n".join(
        [
            _held("ansible/initial_setup.yml k3s", 2000, added_by_a_newer_writer=[1]),
            _held("ansible/deploy.yml sonarr", 1000),
            _held("ansible/deploy.yml sonarr", 3000),
        ]
    )
    ok, msg = checks.gitops.gitops_status(cfg, "deadbeefcafe", owed=owed)
    assert not ok
    assert msg.endswith(
        "2 planes unapplied: ansible/deploy.yml sonarr; ansible/initial_setup.yml k3s"
    )


def test_a_hold_plane_ledger_line_without_a_held_sha_pages_nothing(cfg):
    """`hold_sha` decides whether the page fires; the planes only say what it waits on."""
    owed = _held("ansible/deploy.yml sonarr", 1000)
    ok, _ = checks.gitops.gitops_status(cfg, None, now=_LATE, owed=owed)
    assert ok


def test_a_hold_on_several_planes_counts_each_entry_the_clear_waits_for(cfg):
    """The `hold_plane` class holds one ledger line per failed apply, each cleared on its own.

    The SHA is the newest failure's, not each entry's. A Clear after re-running only the
    newest plane would erase an earlier one still unapplied, so the page counts what is owed.
    It names the deploy UI's Clear and `gitops_state.py clear-hold` with the full SHA, not an
    rm: the planes are `owed` ledger lines (#3392), and an rm of `hold_sha` would leave them to
    re-hold the next failure.
    """
    ok, msg = checks.gitops.gitops_status(
        cfg,
        "deadbeefcafe",
        owed="\n".join(
            [
                _held("ansible/deploy.yml radarr", 1),
                _held("ansible/initial_setup.yml gitops_deploy", 2),
            ]
        ),
    )
    assert not ok
    assert "ansible/deploy.yml radarr" in msg
    assert "ansible/initial_setup.yml gitops_deploy" in msg
    assert "2 planes unapplied" in msg
    assert "Clear the hold in the deploy UI" in msg
    assert f"`{HOLD_CLEAR_CMD} deadbeefcafe`" in msg
    assert " rm " not in msg


def test_a_long_plane_list_cannot_cut_the_clear_command(cfg):
    """`cap_push_msg` cuts from the right, so the command must sit ahead of the plane list."""
    sha = "2d25ced3" * 5
    ok, msg = checks.gitops.gitops_status(
        cfg,
        sha,
        owed="\n".join(
            _held(f"ansible/initial_setup.yml role_{i}", i) for i in range(40)
        ),
    )
    assert not ok
    capped = cap_push_msg(msg)
    assert len(msg) > len(capped), "the list must be long enough to be cut"
    assert f"`{HOLD_CLEAR_CMD} {sha}`" in capped
