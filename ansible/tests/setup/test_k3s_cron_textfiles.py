"""The k3s crons that M6's batch B1 moves write their verdict to the node_exporter textfile.

The plan is docs/target-architecture/monitoring/m6-retire-kuma.md (*B1* and the recipe M3's
disk-health proved). Each producer sources homelab-check-lib.sh with a guard that exits 1, and
writes the three `homelab_check_*` series through the library's `write_check_textfile` before its
Kuma push. The Kuma push stays until the cutover. The real template is rendered and run under bash
with the real library, so the file the case reads is the file the collector would. Only the Kuma
library, the env files, `uv`, `git` and `logger` are stubs. live_drift_check.py is Python and has its
cases in ansible/roles/setup/k3s/tests/test_live_drift_check.py. The deadline literals are pinned
to the render in ansible/tests/services/test_b1_rule_literals.py.
"""

import re

import pytest
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, run, write_exec
from lib.repo_paths import REPO

KUMA_LIB = "/usr/local/lib/kuma-push-lib.sh"
CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"
REAL_CHECK_LIB = REPO / "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"
KUMA_ENV = "/etc/rancher/k3s/kuma-push.env"
HC_ENV = "/etc/healthchecks/ping.env"
UV = "/usr/local/bin/uv"
SHELL_PRODUCERS = {
    "longhorn-backup-health": "longhorn-backup-health.sh.j2",
    "etcd-snapshot-offbox": "etcd-snapshot-offbox.sh.j2",
    "manifest-prune-check": "manifest-prune-check.sh.j2",
    "release-staleness-check": "release-staleness-check.sh.j2",
}

# kuma_push records its status and whether the textfile already existed when the push ran.
KUMA_STUB = """\
kuma_push() {
  local written=no
  [[ -f "$TEXTFILE_DIR/$KUMA_TAG.prom" ]] && written=yes
  printf '%s %s\\n' "$1" "$written" >> "$KUMA_PUSH_OUT"
}
KUMA_PUSH_OK=1
"""
# The real library, with the three things a runner's host decides pinned to what the case sets.
CHECK_LIB_STUB = f"""\
. {REAL_CHECK_LIB}
boot_grace_active() {{ [[ "${{BOOT_GRACE:-0}}" == "1" ]]; }}
wan_reachable() {{ [[ "${{WAN_UP:-1}}" == "1" ]]; }}
"""


def _run(tmp_path, tag, overrides=None, stubs=None, env=None, uv_body="exit 0\n"):
    """Run the rendered producer once; return (textfile dir, every push as (status, written))."""
    body = render_shell_script(
        "setup", "k3s", SHELL_PRODUCERS[tag], overrides=overrides
    )
    kuma_lib, check_lib = tmp_path / "kuma-push-lib.sh", tmp_path / "check-lib.sh"
    kuma_lib.write_text(KUMA_STUB)
    check_lib.write_text(CHECK_LIB_STUB)
    token_env, hc_env = tmp_path / "kuma-push.env", tmp_path / "ping.env"
    token_env.write_text("".join(f"{n}=tok\n" for n in _TOKENS))
    hc_env.write_text("")
    uv = tmp_path / "uv"
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    for literal in (KUMA_LIB, CHECK_LIB, KUMA_ENV):
        assert literal in body, f"{SHELL_PRODUCERS[tag]} no longer references {literal}"
    body = (
        body.replace(KUMA_LIB, str(kuma_lib))
        .replace(CHECK_LIB, str(check_lib))
        .replace(KUMA_ENV, str(token_env))
        .replace(HC_ENV, str(hc_env))
        .replace(UV, str(uv))
    )
    script = write_exec(tmp_path / f"{tag}.sh", body)
    bin_dir = fake_bin(
        tmp_path / "bin",
        curl="exit 0\n",
        logger=f'echo "$@" >> "{tmp_path / "logged"}"\n',
        **(stubs or {}),
    )
    write_exec(uv, uv_body)
    run(
        ["bash", str(script)],
        env={
            "TEXTFILE_DIR": str(textfile_dir),
            "KUMA_PUSH_OUT": str(pushed),
            "KUMA_TAG": tag,
            "PATH": "/usr/bin:/bin",
            **(env or {}),
        },
        stub_bin=bin_dir,
        check=True,
    )
    pushes = [tuple(line.split()) for line in pushed.read_text().splitlines()]
    return textfile_dir, pushes


_TOKENS = (
    "LONGHORN_BACKUP_PUSH_TOKEN",
    "ETCD_SNAPSHOT_PUSH_TOKEN",
    "MANIFEST_PRUNE_PUSH_TOKEN",
    "RELEASE_STALENESS_PUSH_TOKEN",
)


def _series(textfile_dir, tag) -> dict[str, str]:
    lines = (textfile_dir / f"{tag}.prom").read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


@pytest.mark.parametrize("tag", sorted(SHELL_PRODUCERS))
def test_a_producer_sources_the_library_with_a_guard_that_exits_and_writes_for_its_own_check(
    tag,
):
    body = render_shell_script("setup", "k3s", SHELL_PRODUCERS[tag])
    assert re.search(
        r"source /usr/local/lib/homelab-check-lib\.sh \|\| \{\n[^}]*exit 1\n\}", body
    )
    assert f'write_check_textfile {tag} "$SUCCESS"' in body or (
        f'write_check_textfile {tag} "$success"' in body
    )


def test_a_down_verdict_is_written_before_the_push_and_carries_the_daily_deadline(
    tmp_path,
):
    # A disarmed etcd snapshot reports down through report(), the one path every verdict takes.
    textfile_dir, pushes = _run(
        tmp_path,
        "etcd-snapshot-offbox",
        overrides={"k3s_etcd_s3_armed": False},
    )
    series = _series(textfile_dir, "etcd-snapshot-offbox")
    assert series['homelab_check_success{check="etcd-snapshot-offbox"}'] == "0"
    assert series['homelab_check_deadline_seconds{check="etcd-snapshot-offbox"}'] == (
        "90000"
    )
    assert pushes == [("down", "yes")]


def test_a_reader_verdict_is_written_and_a_boot_grace_run_writes_nothing(tmp_path):
    reader = "printf 'down\\tbackups in Error state\\n'\n"
    textfile_dir, pushes = _run(tmp_path, "longhorn-backup-health", uv_body=reader)
    series = _series(textfile_dir, "longhorn-backup-health")
    assert series['homelab_check_success{check="longhorn-backup-health"}'] == "0"
    assert series['homelab_check_deadline_seconds{check="longhorn-backup-health"}'] == (
        "1500"
    )
    assert pushes == [("down", "yes")]

    skipped = tmp_path / "skipped"
    skipped.mkdir()
    textfile_dir, pushes = _run(
        skipped, "longhorn-backup-health", uv_body=reader, env={"BOOT_GRACE": "1"}
    )
    assert list(textfile_dir.iterdir()) == []


def test_a_failed_fetch_is_the_runs_verdict_and_follows_the_wan_probe(tmp_path):
    git = {"git": "echo 'fatal: unable to access origin' >&2\nexit 128\n"}
    # The WAN answers, so the refused fetch is a real fault: success 0.
    textfile_dir, _ = _run(tmp_path, "release-staleness-check", stubs=git)
    key = 'homelab_check_success{check="release-staleness-check"}'
    assert _series(textfile_dir, "release-staleness-check")[key] == "0"
    # The WAN is down, so the check reports up with a skip note and the WAN check owns the page.
    offline = tmp_path / "offline"
    offline.mkdir()
    textfile_dir, _ = _run(
        offline, "release-staleness-check", stubs=git, env={"WAN_UP": "0"}
    )
    assert _series(textfile_dir, "release-staleness-check")[key] == "1"
