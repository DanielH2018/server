"""The setup-plane drift reader for hosts that run no manifest-prune-check (2026-08-29 M-10).

THE GAP: manifest-prune-check.sh is installed by roles/setup/k3s/tasks/health-crons.yml, imported
only from that role's main.yml, which k3s-bringup.yml asserts onto k3s_server_hosts. So it exists
on daniel-box and nowhere else — while daniel-server renders the whole UPS shutdown chain. Live on
2026-08-29: daniel-server had no /var/lib/homelab/setup-render-manifest.d at all, and its
/etc/nut/upsmon.conf was dated Aug 17 against a template changed on 2026-08-28.

THE REFUTED FIX, and why the tests below are shaped the way they are: adding the stamp to
roles/nut_host was vetted LAUNDERS, because it writes a fragment on a host with no reader. So
every arm here is EXECUTED against a real fixture rather than asserted from the script's text —
a shell library is only ever observed passing, and the accept half alone cannot tell a working
arm from one that fires on nothing.
"""

import re
import subprocess
from pathlib import Path

import yaml
from _helpers import REPO

_REPO = REPO
_LIB = _REPO / "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
_CHECK = _REPO / "ansible/roles/setup/initial_setup/templates/setup-drift-check.sh.j2"
_CRONS = _REPO / "ansible/roles/setup/initial_setup/tasks/crons.yml"
_GROUP_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_TILE = _REPO / "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
_ROTATION = _REPO / "scripts/secrets_mgmt/secret_rotation.py"


def _run_scan(tmp_path, deployed=(), rendered=(), repo_files=None):
    """Source the library and run setup_drift_scan against a fixture, returning its four outputs.

    `deployed` and `rendered` are fragment bodies; `repo_files` maps a repo-relative path to its
    content, so a caller can make a stamp agree or disagree with the tree.
    """
    repo = tmp_path / "repo"
    for rel, content in (repo_files or {}).items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    dep_dir = tmp_path / "deployed.d"
    ren_dir = tmp_path / "render.d"
    dep_dir.mkdir()
    ren_dir.mkdir()
    for i, body in enumerate(deployed):
        (dep_dir / f"frag{i}").write_text(body)
    for i, body in enumerate(rendered):
        (ren_dir / f"frag{i}").write_text(body)

    script = tmp_path / "drive.sh"
    script.write_text(
        "set -uo pipefail\n"
        f"DEPLOYED_MANIFEST_DIR={dep_dir}\n"
        f"RENDER_MANIFEST_DIR={ren_dir}\n"
        f"REPO_DIR={repo}\n"
        f"source {_LIB}\n"
        "setup_drift_scan\n"
        'printf "DRIFTED=%s\\nSTALE=%s\\nDEPLOYED_NOTE=%s\\nMANIFEST_NOTE=%s\\n" '
        '"$DRIFTED" "$STALE" "$DEPLOYED_NOTE" "$MANIFEST_NOTE"\n'
    )
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.strip().splitlines())


def _source(path: Path) -> str:
    """The script minus its comment lines.

    These scripts explain themselves at length, and the explanations name the very paths and
    calls the assertions below forbid or order — so a text search over the whole file matches
    the prose and reports a defect that is not there. Same trap as the UPS watchdog's own guard
    on 2026-08-29.
    """
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _sha(path: Path) -> str:
    return subprocess.run(
        ["sha256sum", str(path)], capture_output=True, text=True, check=True
    ).stdout.split()[0]


def _old_repo(tmp_path, permissive_system_config=False):
    """A one-commit repo dated 2020, and the env that reproduces it. Returns (repo, env).

    The env pins every git config scope the fixture does not own, because the ownership tests
    below are decided by exactly those scopes. A `safe.directory = *` in the SYSTEM config makes
    git accept any repo, which suppresses the refusal those tests are built on — that is not
    hypothetical, it is how this pair failed on the GitHub runner while passing on a host with
    no /etc/gitconfig. `permissive_system_config` reproduces the runner deliberately, so the
    neutralisation has a test of its own rather than being an unproven precaution.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    system_config = tmp_path / "gitconfig.system"
    system_config.write_text("[safe]\n\tdirectory = *\n")
    global_config = tmp_path / "gitconfig.global"
    global_config.write_text("")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_SYSTEM": str(system_config),
        "GIT_CONFIG_GLOBAL": str(global_config),
    }
    if not permissive_system_config:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "old", "--no-gpg-sign"],
        check=True,
        env=env,
    )
    return repo, env


# ── arm 3: the render-staleness arm, the one M-10 is about ────────────────────────────────────


def test_a_current_render_is_clean(tmp_path):
    repo_files = {"ansible/roles/nut_host/templates/host-upsmon.conf.j2": "MONITOR x\n"}
    repo = tmp_path / "repo"
    (repo / "ansible/roles/nut_host/templates").mkdir(parents=True)
    tpl = repo / "ansible/roles/nut_host/templates/host-upsmon.conf.j2"
    tpl.write_text("MONITOR x\n")
    frag = f"ansible/roles/nut_host/templates/host-upsmon.conf.j2 {_sha(tpl)}\n"
    got = _run_scan(tmp_path, rendered=[frag], repo_files=repo_files)
    assert got["STALE"] == ""
    assert got["MANIFEST_NOTE"] == "", (
        "an armed arm must not also report itself unarmed"
    )


def test_a_changed_template_is_reported_stale(tmp_path):
    """The rejecting half, and the exact live shape: the stamp records what the host rendered,
    the tree has moved on, and nothing else on daniel-server can see the difference."""
    repo = tmp_path / "repo"
    (repo / "ansible/roles/nut_host/templates").mkdir(parents=True)
    tpl = repo / "ansible/roles/nut_host/templates/host-upsmon.conf.j2"
    tpl.write_text("MONITOR x\n")
    stale_sha = _sha(tpl)
    tpl.write_text("MONITOR x\nMONITOR y\n")  # the 2026-08-28 template change
    frag = f"ansible/roles/nut_host/templates/host-upsmon.conf.j2 {stale_sha}\n"
    got = _run_scan(tmp_path, rendered=[frag])
    assert "host-upsmon.conf.j2" in got["STALE"]


def test_a_template_deleted_from_the_repo_is_reported(tmp_path):
    frag = "ansible/roles/nut_host/templates/gone.j2 " + "0" * 64 + "\n"
    got = _run_scan(tmp_path, rendered=[frag])
    assert "template gone from the repo" in got["STALE"]


# ── arm 2: the deployed-code arm ──────────────────────────────────────────────────────────────


def test_matching_deployed_code_is_clean(tmp_path):
    live = tmp_path / "live.sh"
    live.write_text("echo hi\n")
    repo = tmp_path / "repo"
    (repo / "ansible/files").mkdir(parents=True)
    (repo / "ansible/files/lib.sh").write_text("echo hi\n")
    got = _run_scan(tmp_path, deployed=[f"{live} ansible/files/lib.sh\n"])
    assert got["DRIFTED"] == ""


def test_a_stale_deployed_copy_is_reported(tmp_path):
    live = tmp_path / "live.sh"
    live.write_text("echo OLD\n")
    repo = tmp_path / "repo"
    (repo / "ansible/files").mkdir(parents=True)
    (repo / "ansible/files/lib.sh").write_text("echo NEW\n")
    got = _run_scan(tmp_path, deployed=[f"{live} ansible/files/lib.sh\n"])
    assert str(live) in got["DRIFTED"]


def test_a_source_deleted_from_the_repo_is_drift_not_an_exemption(tmp_path):
    live = tmp_path / "live.sh"
    live.write_text("echo hi\n")
    got = _run_scan(tmp_path, deployed=[f"{live} ansible/files/gone.sh\n"])
    assert "source gone from the repo" in got["DRIFTED"]


# ── the unarmed guards: an empty or absent directory must never read as a pass ─────────────────


def test_an_absent_manifest_reads_as_unarmed_not_clean(tmp_path):
    got = _run_scan(tmp_path)
    assert got["DRIFTED"] == "" and got["STALE"] == ""
    assert "absent or empty" in got["DEPLOYED_NOTE"]
    assert "absent or empty" in got["MANIFEST_NOTE"]


def test_a_zero_byte_fragment_cannot_disarm_an_arm(tmp_path):
    """L2, executed rather than grepped.

    WHAT THIS DOES AND DOES NOT PROVE, because the distinction was measured rather than assumed:
    the `-s` fragment guard is NOT what saves this case. Mutating it back to `-r` leaves both
    assertions below passing, because an empty file contributes no lines either way and the
    ENTRY COUNTER is what turns "no lines" into an unarmed note. The counter is the load-bearing
    guard and this is its red-proof; `-s` is belt-and-braces and is pinned textually by
    test_an_empty_fragment_cannot_disarm_the_arm in test_setup_render_manifest.py.
    """
    got = _run_scan(tmp_path, deployed=[""], rendered=[""])
    assert "absent or empty" in got["MANIFEST_NOTE"], (
        "an empty fragment must leave the arm UNARMED, not silently pass it"
    )
    assert "absent or empty" in got["DEPLOYED_NOTE"]


# ── the tree-freshness arm: what stops this fix laundering its own finding ─────────────────────


def test_the_tree_age_arm_reads_the_checkout(tmp_path):
    """arm 3 compares a render against the tree on THIS host, and a host without gitops-deploy
    does not refresh that tree — daniel-server was 39 commits behind on 2026-08-17. A stale
    checkout makes the stamp and the template agree, so the arm reads green exactly when the
    host is furthest behind. The age is reported so that cannot pass unnoticed."""
    repo, env = _old_repo(tmp_path)
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={repo}\nsource {_LIB}\nsetup_drift_tree_age_days\n"
    )
    age = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()
    assert int(age) > 1800, "a 2020 commit must read as thousands of days old"


def test_an_unreadable_checkout_is_a_fault_not_a_pass(tmp_path):
    """The rejecting half of the arm above: with no age the arms below it cannot be trusted, so
    the honest verdict is 'could not read the tree', never 'nothing has drifted'."""
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={tmp_path}/nope\nsource {_LIB}\n"
        "setup_drift_tree_age_days; echo rc=$?\n"
    )
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=True
    ).stdout
    assert "rc=1" in out, "an unreadable checkout must fail, not print a plausible age"
    text = _source(_CHECK)
    assert "cannot read the checkout" in text and "STATUS=down" in text, (
        "the check must turn an unreadable checkout into a DOWN, not a silent green"
    )


# ── the arm under the uid it actually runs as ─────────────────────────────────────────────────
#
# The two tests above run git as the user that owns the fixture, which is the precise reason
# they were green while the arm was dead. The cron runs as root against an ubuntu-owned
# checkout, git refuses on dubious ownership, and `2>/dev/null` hides the reason — so every
# real run reported "cannot read the checkout" and the tile sat DOWN from the day it shipped.
# GIT_TEST_ASSUME_DIFFERENT_OWNER=1 is git's own hook for forcing that path without a second
# uid, so the pair below can run unprivileged in CI.


def test_a_permissive_system_gitconfig_defeats_the_refusal(tmp_path):
    """Why the fixture pins GIT_CONFIG_NOSYSTEM, proven rather than asserted.

    A `safe.directory = *` in the system config makes git accept a repo it would otherwise
    refuse, so the control below reports no refusal and the pair silently stops testing
    anything. That is the shape of the CI failure on 2026-08-30: green on a host with no
    /etc/gitconfig, red on the GitHub runner."""
    repo, env = _old_repo(tmp_path, permissive_system_config=True)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    done = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode == 0, (
        "a permissive system config no longer suppresses the ownership refusal, so the "
        "GIT_CONFIG_NOSYSTEM pin in the fixture is now guarding nothing"
    )


def test_a_foreign_owned_checkout_refuses_a_bare_git_read(tmp_path):
    """The CONTROL for the test below, and the red half of the pair.

    Without it, asserting that the helper returns an age under GIT_TEST_ASSUME_DIFFERENT_OWNER
    proves nothing: an env var that silently did nothing would leave that test green for the
    same bad reason the original pair was green. This pins the simulation by showing the bare
    read — the code as it shipped — does fail."""
    repo, env = _old_repo(tmp_path)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    done = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode != 0, (
        "GIT_TEST_ASSUME_DIFFERENT_OWNER no longer forces the dubious-ownership refusal, so "
        "the test below is not exercising the failure it claims to cover"
    )
    assert "dubious ownership" in done.stderr


def test_the_tree_age_arm_reads_a_foreign_owned_checkout(tmp_path):
    """The accept half: under the same refusal the helper must still return an age.

    This is the arm as the cron runs it. It fails against the pre-2026-08-30 helper, which
    called git with no safe.directory exception."""
    repo, env = _old_repo(tmp_path)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={repo}\nsource {_LIB}\nsetup_drift_tree_age_days\n"
    )
    age = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()
    assert age, (
        "the arm reported no age at all — the ownership refusal is still fatal to it"
    )
    assert int(age) > 1800, "a 2020 commit must read as thousands of days old"


# ── wiring: the halves that have to agree across four files ───────────────────────────────────


def test_the_reader_is_armed_where_no_manifest_prune_check_runs():
    """The whole finding is a host that renders and is never read. daniel-server must be in the
    allowlist, and daniel-box must not be — it already has manifest-prune-check, and a second
    reader there would page twice for one drift."""
    gv = yaml.safe_load(_GROUP_VARS.read_text())
    hosts = gv["setup_drift_check_hosts"]
    assert "daniel-server" in hosts, (
        "daniel-server renders roles/nut_host — the UPS shutdown chain — and is the host the "
        "finding is about"
    )
    for server in gv["k3s_server_hosts"]:
        assert server not in hosts, (
            f"{server} already runs manifest-prune-check; a second reader double-pages one drift"
        )


def test_every_reader_task_is_gated_on_that_allowlist():
    """A task that forgets the gate installs a root cron on every host, including the Pi, which
    stamps nothing and would report only that it is unarmed — a permanently unhelpful monitor."""
    tasks = yaml.safe_load(_CRONS.read_text())
    named = [t for t in tasks if "setup_drift" in str(t.get("tags", ""))]
    assert named, "the setup-drift tasks lost their tag family"
    for task in named:
        assert task.get("when") == "inventory_hostname in setup_drift_check_hosts", (
            f"'{task['name']}' is not gated on setup_drift_check_hosts"
        )


def test_the_check_stamps_its_own_templates():
    """Otherwise the reader is the one rendered script nothing can report stale — the finding,
    rebuilt inside its own fix."""
    text = _CRONS.read_text()
    assert "stamp_render_name: setup-drift-check" in text
    for tpl in ("setup-drift-check.sh.j2", "setup-drift-kuma-push.env.j2"):
        assert f"templates/{tpl}" in text, f"{tpl} is rendered but never stamped"


def test_the_library_is_stamped_as_deployed_code():
    text = _CRONS.read_text()
    assert "stamp_deployed_name: setup-drift-lib" in text, (
        "the shared arms are copy:-deployed, so a stale copy of them silently changes what both "
        "readers check — the exact class arm 2 exists to report"
    )


def test_the_tile_is_gated_on_the_token():
    """An ungated tile sits red from creation until /add-secret runs, because the cron skips its
    push while the token is empty — and kuma-drift counts a declared-but-never-beating monitor
    as missing."""
    tile = _TILE.read_text()
    assert "setup-drift-check.json" in tile, "the pusher has no Kuma monitor to push to"
    gate = re.search(
        r"\{% if setup_drift_push_token \| default\(''\) %\}(.*?)\{% endif %\}",
        tile,
        re.DOTALL,
    )
    assert gate and "setup-drift-check.json" in gate.group(1), (
        "the tile must sit inside the token gate, like its siblings"
    )


def test_the_token_is_registered_as_cross_host():
    """The cron runs on daniel-server and the tile deploys from daniel-box, so no single
    `rotate --deploy` can move both halves. Left out of the set, an unattended rotation would
    update the tile and leave the cron pushing the old value — silencing the only monitor that
    can report a stale render on the host that owns the UPS shutdown chain."""
    assert '"setup_drift_push_token"' in _ROTATION.read_text(), (
        "setup_drift_push_token must be in CROSS_HOST_PUSH_TOKENS"
    )


def test_the_reader_does_not_claim_the_orphan_arm():
    """manifest-prune-check's first arm needs /etc/rancher/k3s/manifests and the control plane's
    staged set; an agent node has neither. An arm that structurally cannot fire is worse than no
    arm, because it reads as coverage."""
    text = _source(_CHECK)
    assert "/etc/rancher/k3s/manifests" not in text
    assert "kubectl" not in text


def test_the_reader_logs_before_it_pushes():
    """A successfully-pushed DOWN otherwise leaves no durable record, and `probe.py alerts`
    reconstructs host-cron episodes by matching status=down in syslog. NOTICE, not INFO:
    journald here caps MaxLevelStore=notice."""
    text = _source(_CHECK)
    logger_at = text.index("logger -p daemon.notice -t setup-drift-check")
    assert logger_at < text.index("kuma_push "), (
        "the durable record must precede the push"
    )
    assert "status=${STATUS}" in text


def test_the_token_is_sourced_never_inlined():
    """The script lands 0755, so an inlined token is readable by every local account."""
    text = _source(_CHECK)
    assert "{{ setup_drift_push_token }}" not in text, (
        "the token must come from the 0640 env file, not be rendered into a 0755 script"
    )
    assert "/etc/homelab/kuma-push.env" in text
    assert "/etc/rancher/k3s/kuma-push.env" not in text, (
        "that file is a whole-content template owned by roles/setup/k3s and does not exist on "
        "an agent node at all"
    )
