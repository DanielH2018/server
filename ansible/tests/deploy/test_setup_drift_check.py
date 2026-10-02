"""The setup-plane drift reader for hosts that run no manifest-prune-check.

THE GAP: manifest-prune-check.sh is installed by roles/setup/k3s/tasks/health-crons.yml, imported
only from that role's main.yml, which k3s-bringup.yml asserts onto k3s_server_hosts. So it exists
on daniel-box and nowhere else — while daniel-server renders the whole UPS shutdown chain.

WHY THE TESTS ARE SHAPED AS THEY ARE: adding the stamp to roles/setup/nut_host alone would
launder the finding, because it writes a fragment on a host with no reader. So every arm here is EXECUTED against a real fixture rather than asserted from the script's text —
a shell library is only ever observed passing, and the accept half alone cannot tell a working
arm from one that fires on nothing.
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import REPO
from _k8s_render import render_role_template
from _shell_render import render_shell_script, rendered_shell_text
from lib.proc_testing import run

_REPO = REPO
_LIB = _REPO / "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
_CHECK = ("setup", "initial_setup", "setup-drift-check.sh.j2")
_CRONS = _REPO / "ansible/roles/setup/initial_setup/tasks/crons.yml"
_GROUP_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_CONSUMERS = _REPO / "scripts/secrets_mgmt/consumers.py"
_SENTINEL = "inlined-token-sentinel"


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
    out = run(["bash", str(script)], check=True).stdout
    return dict(line.split("=", 1) for line in out.strip().splitlines())


def _no_comments(text: str) -> str:
    """`text` minus its comment lines.

    These scripts explain themselves at length, and the explanations name the very paths and
    calls the assertions below forbid or order — so a text search over the whole file matches
    the prose and reports a defect that is not there.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _check_script(overrides: dict | None = None) -> str:
    """The drift check as a host runs it: rendered, then stripped of its comments.

    Rendered rather than read off the template, because a value that moves into a role default
    leaves a source assertion matching `{{ ... }}`, and a pattern that matches nothing passes
    (#3178). `overrides` renders the one template again with a value the inventory does not
    hold; without it the whole roster's cached render answers.
    """
    text = (
        rendered_shell_text(*_CHECK)
        if overrides is None
        else render_shell_script(*_CHECK, overrides)
    )
    return _no_comments(text)


def _sha(path: Path) -> str:
    return run(["sha256sum", str(path)], check=True).stdout.split()[0]


def _old_repo(tmp_path, permissive_system_config=False):
    """A one-commit repo dated 2020, and the env that reproduces it. Returns (repo, env).

    The env pins every git config scope the fixture does not own, because the ownership tests
    below are decided by exactly those scopes. A `safe.directory = *` in the SYSTEM config makes
    git accept any repo, which suppresses the refusal those tests are built on — a runner
    with that config behaves differently from a host with no /etc/gitconfig. `permissive_system_config` reproduces the runner deliberately, so the
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
    run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f").write_text("x")
    run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
    run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "old", "--no-gpg-sign"],
        check=True,
        env=env,
    )
    return repo, env


# ── arm 3: the render-staleness arm ────────────────────────────────────


def test_a_current_render_is_clean(tmp_path):
    repo_files = {
        "ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2": "MONITOR x\n"
    }
    repo = tmp_path / "repo"
    (repo / "ansible/roles/setup/nut_host/templates").mkdir(parents=True)
    tpl = repo / "ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2"
    tpl.write_text("MONITOR x\n")
    frag = f"ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2 {_sha(tpl)}\n"
    got = _run_scan(tmp_path, rendered=[frag], repo_files=repo_files)
    assert got["STALE"] == ""
    assert got["MANIFEST_NOTE"] == "", (
        "an armed arm must not also report itself unarmed"
    )


def test_a_changed_template_is_reported_stale(tmp_path):
    """The rejecting half, and the exact live shape.

    The stamp records what the host rendered, the tree has moved on, and nothing else on
    daniel-server can see the difference.
    """
    repo = tmp_path / "repo"
    (repo / "ansible/roles/setup/nut_host/templates").mkdir(parents=True)
    tpl = repo / "ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2"
    tpl.write_text("MONITOR x\n")
    stale_sha = _sha(tpl)
    tpl.write_text("MONITOR x\nMONITOR y\n")
    frag = f"ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2 {stale_sha}\n"
    got = _run_scan(tmp_path, rendered=[frag])
    assert "host-upsmon.conf.j2" in got["STALE"]


def test_a_template_deleted_from_the_repo_is_reported(tmp_path):
    frag = "ansible/roles/setup/nut_host/templates/gone.j2 " + "0" * 64 + "\n"
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
    """Executed rather than grepped.

    WHAT THIS DOES AND DOES NOT PROVE: the `-s` fragment guard is NOT what saves this case. Mutating it back to `-r` leaves both
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
    does not refresh that tree. A stale
    checkout makes the stamp and the template agree, so the arm reads green exactly when the
    host is furthest behind. The age is reported so that cannot pass unnoticed."""
    repo, env = _old_repo(tmp_path)
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={repo}\nsource {_LIB}\nsetup_drift_tree_age_days\n"
    )
    age = run(["bash", str(script)], check=True, env=env).stdout.strip()
    assert int(age) > 1800, "a 2020 commit must read as thousands of days old"


def test_an_unreadable_checkout_is_a_fault_not_a_pass(tmp_path):
    """The rejecting half of the arm above: an unreadable checkout is a fault, not a pass.

    With no age the arms below it cannot be trusted, so the honest verdict is 'could not read the
    tree', never 'nothing has drifted'.
    """
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={tmp_path}/nope\nsource {_LIB}\n"
        "setup_drift_tree_age_days; echo rc=$?\n"
    )
    out = run(["bash", str(script)], check=True).stdout
    assert "rc=1" in out, "an unreadable checkout must fail, not print a plausible age"
    text = _check_script()
    assert "cannot read the checkout" in text and "STATUS=down" in text, (
        "the check must turn an unreadable checkout into a DOWN, not a silent green"
    )


# ── the arm under the uid it actually runs as ─────────────────────────────────────────────────
#
# The two tests above run git as the user that owns the fixture, which is the precise reason
# they would stay green while the arm is dead. The cron runs as root against an ubuntu-owned
# checkout, git refuses on dubious ownership, and `2>/dev/null` hides the reason — so every
# real run would report "cannot read the checkout" and the tile would sit DOWN.
# GIT_TEST_ASSUME_DIFFERENT_OWNER=1 is git's own hook for forcing that path without a second
# uid, so the pair below can run unprivileged in CI.


def test_a_permissive_system_gitconfig_defeats_the_refusal(tmp_path):
    """Why the fixture pins GIT_CONFIG_NOSYSTEM, proven rather than asserted.

    A `safe.directory = *` in the system config makes git accept a repo it would otherwise
    refuse, so the control below reports no refusal and the pair silently stops testing
    anything. That is the shape of a CI-only failure: green on a host with no
    /etc/gitconfig, red on a runner that has one.
    """
    repo, env = _old_repo(tmp_path, permissive_system_config=True)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    done = run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], env=env)
    assert done.returncode == 0, (
        "a permissive system config no longer suppresses the ownership refusal, so the "
        "GIT_CONFIG_NOSYSTEM pin in the fixture is now guarding nothing"
    )


def test_a_foreign_owned_checkout_refuses_a_bare_git_read(tmp_path):
    """The CONTROL for the test below, and the red half of the pair.

    Without it, asserting that the helper returns an age under GIT_TEST_ASSUME_DIFFERENT_OWNER
    proves nothing: an env var that silently did nothing would leave that test green for no good
    reason. This pins the simulation by showing the bare read does fail.
    """
    repo, env = _old_repo(tmp_path)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    done = run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], env=env)
    assert done.returncode != 0, (
        "GIT_TEST_ASSUME_DIFFERENT_OWNER no longer forces the dubious-ownership refusal, so "
        "the test below is not exercising the failure it claims to cover"
    )
    assert "dubious ownership" in done.stderr


def test_the_tree_age_arm_reads_a_foreign_owned_checkout(tmp_path):
    """The accept half: under the same refusal the helper must still return an age.

    This is the arm as the cron runs it. It fails against a helper that calls git with
    no safe.directory exception.
    """
    repo, env = _old_repo(tmp_path)
    env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    script = tmp_path / "age.sh"
    script.write_text(
        f"set -uo pipefail\nREPO_DIR={repo}\nsource {_LIB}\nsetup_drift_tree_age_days\n"
    )
    age = run(["bash", str(script)], check=True, env=env).stdout.strip()
    assert age, (
        "the arm reported no age at all — the ownership refusal is still fatal to it"
    )
    assert int(age) > 1800, "a 2020 commit must read as thousands of days old"


# ── wiring: the halves that have to agree across four files ───────────────────────────────────


def test_the_reader_is_armed_where_no_manifest_prune_check_runs():
    """The whole finding is a host that renders and is never read.

    daniel-server must be in the allowlist, and daniel-box must not be — it already has
    manifest-prune-check, and a second reader there would page twice for one drift.
    """
    gv = yaml_fast.safe_load(_GROUP_VARS.read_text())
    hosts = gv["setup_drift_check_hosts"]
    assert "daniel-server" in hosts, (
        "daniel-server renders roles/setup/nut_host — the UPS shutdown chain — and is the host the "
        "finding is about"
    )
    for server in gv["k3s_server_hosts"]:
        assert server not in hosts, (
            f"{server} already runs manifest-prune-check; a second reader double-pages one drift"
        )


def test_every_reader_task_is_gated_on_that_allowlist():
    """A task that forgets the gate installs a root cron on every host, including the Pi, which
    stamps nothing and would report only that it is unarmed — a permanently unhelpful monitor."""
    tasks = yaml_fast.safe_load(_CRONS.read_text())
    named = [t for t in tasks if "setup_drift" in str(t.get("tags", ""))]
    assert named, "the setup-drift tasks lost their tag family"
    for task in named:
        if str(task.get("ansible.builtin.import_tasks", "")).endswith(
            "common/tasks/kuma_check_timer.yml"
        ):
            # The schedule is a kuma-check timer import, which runs on EVERY host so its
            # absent arm can remove the timer from one dropped off the list; the gate is the
            # state expression instead of a `when`.
            state = str(task["vars"]["kuma_check_state"])
            assert "inventory_hostname in setup_drift_check_hosts" in state, (
                f"'{task['name']}' does not follow setup_drift_check_hosts"
            )
            continue
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

    def tile(token: str) -> str:
        return render_role_template(
            "uptime-kuma",
            "static-monitors.yaml.j2",
            {"setup_drift_push_token": token},
        )

    assert "setup-drift-check.json" in tile("drift-token"), (
        "the pusher has no Kuma monitor to push to"
    )
    assert "setup-drift-check.json" not in tile(""), (
        "the tile must sit inside the token gate, like its siblings"
    )


def test_the_token_is_registered_as_cross_host():
    """The cron runs on daniel-server and the tile deploys from daniel-box, so no single
    `rotate --deploy` can move both halves. Left out of the set, an unattended rotation would
    update the tile and leave the cron pushing the old value — silencing the only monitor that
    can report a stale render on the host that owns the UPS shutdown chain."""
    assert '"setup_drift_push_token"' in _CONSUMERS.read_text(), (
        "setup_drift_push_token must be in CROSS_HOST_PUSH_TOKENS"
    )


def test_the_reader_does_not_claim_the_orphan_arm():
    """manifest-prune-check's first arm needs /etc/rancher/k3s/manifests and the control plane's
    staged set; an agent node has neither. An arm that structurally cannot fire is worse than no
    arm, because it reads as coverage."""
    text = _check_script()
    assert "/etc/rancher/k3s/manifests" not in text
    assert "kubectl" not in text


def test_the_reader_logs_before_it_pushes():
    """A successfully-pushed DOWN otherwise leaves no durable record, and `probe.py alerts`
    reconstructs host-cron episodes by matching status=down in syslog. NOTICE, not INFO:
    journald here caps MaxLevelStore=notice."""
    text = _check_script()
    logger_at = text.index("logger -p daemon.notice -t setup-drift-check")
    assert logger_at < text.index("kuma_push "), (
        "the durable record must precede the push"
    )
    assert "status=${STATUS}" in text


def test_the_token_is_sourced_never_inlined():
    """The script lands 0755, so an inlined token is readable by every local account.

    The claim is made against a render with the token set to a sentinel, which also fails on an
    alias of it: a source read could only ask whether this one Jinja name appears, and a render
    of the deployed inventory shows every token as `STUB` whether it is inlined or not.

    The control comes first, because a renamed variable would otherwise leave the claim vacuous:
    the override would land on nothing, no render would carry the sentinel, and an inlined token
    under the new name would pass. The env file is the template that legitimately carries the
    token, so the sentinel must appear there.
    """
    env = render_shell_script(
        "setup",
        "initial_setup",
        "setup-drift-kuma-push.env.j2",
        {"setup_drift_push_token": _SENTINEL},
    )
    assert _SENTINEL in env, (
        "the override reached no render — setup_drift_push_token has been renamed, and the "
        "assertion below is now asking nothing"
    )
    text = _check_script({"setup_drift_push_token": _SENTINEL})
    assert _SENTINEL not in text, (
        "the token must come from the 0640 env file, not be rendered into a 0755 script"
    )
    assert "/etc/homelab/kuma-push.env" in text
    assert "/etc/rancher/k3s/kuma-push.env" not in text, (
        "that file is a whole-content template owned by roles/setup/k3s and does not exist on "
        "an agent node at all"
    )
