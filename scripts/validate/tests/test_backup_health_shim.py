"""The rendered longhorn-backup-health shim: arm states, env-var parity, and real runs.

Split out of `test_validate_shell_templates.py`, which keeps the validator machinery. The
tests here render `longhorn-backup-health.sh.j2` and — for most of them — RUN the rendered
script, with every external seam (the Python reader, `kuma_push`, `boot_grace_active`, the
token files, `curl`) replaced by a fixture. `conftest.py` in this directory puts a stubbed
`logger` first on PATH, which is what keeps these runs out of the host's syslog.

Run: uv run pytest scripts/validate/tests/test_backup_health_shim.py
"""

import os
import re
import shutil
import subprocess

import pytest
from validate import shell_templates as v
from lib import shell_lint as sl
from lib.render_guard import ALL_VARS, BASE_CONTEXT, load_yaml

BACKUP_HEALTH = v.ROLES / "setup" / "k3s" / "templates" / "longhorn-backup-health.sh.j2"
BACKUP_HEALTH_READER = (
    v.ANSIBLE / "roles" / "setup" / "k3s" / "files" / "longhorn_backup_health.py"
)
KUMA_PUSH_LIB = (
    v.ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "kuma-push-lib.sh"
)


@pytest.mark.parametrize(
    ("b2_armed", "r2_armed", "expect_backup_armed", "expect_r2_armed"),
    [
        (True, True, "True", "True"),
        (True, False, "True", "False"),
        (False, True, "False", "True"),
        (False, False, "False", "False"),
    ],
)
def test_backup_health_renders_clean_for_every_arm_state(
    tmp_path,
    b2_armed: bool,
    r2_armed: bool,
    expect_backup_armed: str,
    expect_r2_armed: str,
):
    """Both branches of the armed gates must render to valid shell.

    main() renders with group_vars only, so the ROLE default `k3s_longhorn_backup_armed: false`
    is never applied there and its branch would go unexercised — the dead-path shape that let two
    commands stay broken behind passing tests after the k3s cutover. Since the shim only exports
    LONGHORN_BACKUP_ARMED/LONGHORN_R2_ARMED for the Python reader to interpret (BACKUP_TARGETS
    itself is now derived cluster-side), the disarmed branch that matters is the exported string
    the reader parses — a wrong render there disarms silently instead of at `set -u`.
    """
    shellcheck_bin = shutil.which("shellcheck")
    assert shellcheck_bin, "shellcheck must be on PATH (dev dependency shellcheck-py)"

    ctx = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **v.SHELL_STUB_OVERRIDES,
        "k3s_longhorn_backup_armed": b2_armed,
        "k3s_longhorn_r2_armed": r2_armed,
    }
    rendered = sl.render_template(BACKUP_HEALTH, ctx)

    out = tmp_path / "longhorn-backup-health.sh"
    out.write_text(rendered)
    assert sl.bash_syntax_check(out) is None
    assert sl.shellcheck_check(out, shellcheck_bin) is None

    assert f'export LONGHORN_BACKUP_ARMED="{expect_backup_armed}"' in rendered
    assert f'export LONGHORN_R2_ARMED="{expect_r2_armed}"' in rendered


def test_backup_health_arm_gates_treat_the_string_false_as_disarmed():
    # Ansible's `-e k3s_longhorn_backup_armed=false` passes the STRING "false", which is truthy in
    # Jinja. Without `| bool` an extra-vars disarm would render LONGHORN_BACKUP_ARMED="true" and
    # silently restore the permanently-red monitor this gate exists to prevent.
    ctx = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **v.SHELL_STUB_OVERRIDES,
        "k3s_longhorn_backup_armed": "false",
        "k3s_longhorn_r2_armed": "false",
    }
    rendered = sl.render_template(BACKUP_HEALTH, ctx)
    assert 'export LONGHORN_BACKUP_ARMED="False"' in rendered
    assert 'export LONGHORN_R2_ARMED="False"' in rendered


def test_backup_health_logs_unconditionally_even_when_the_reader_itself_breaks():
    """The reader logs its own verdict on a normal run — but on THIS branch it never got that far.

    Without a `logger` call inside the `if ! OUT=$(...)` branch, the one failure mode where the
    local journalctl trail matters most (the Python reader crashing, or `uv` itself missing) is
    the one case that leaves no record at all.
    """
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = sl.render_template(BACKUP_HEALTH, ctx)
    reader_failed_branch = rendered.split("if [[ $RC -ne 0", 1)[1].split("else", 1)[0]
    assert "logger -t longhorn-backup-health" in reader_failed_branch


def test_backup_health_shim_exports_every_env_var_the_reader_requires():
    """LONGHORN_* names are derived from the reader's OWN source, not hardcoded here.

    Every LONGHORN_* var the reader reads is REQUIRED — `_require_env`/`_require_int_env`/
    `_require_bool_env`, no hardcoded fallback (the 2026-09-04 review's finding #3: a fallback
    used to let a shim that stopped exporting one var substitute a stale constant silently). The
    two sides must therefore agree exactly: this derives the required set straight from
    longhorn_backup_health.py's source so a var added to one side without the other is caught
    here, rather than by the reader exiting nonzero in production naming the var nobody remembered
    to export.
    """
    reader_source = BACKUP_HEALTH_READER.read_text()
    required = set(
        re.findall(r'_require_\w*env\("(LONGHORN_[A-Z0-9_]+)"\)', reader_source)
    )
    assert len(required) >= 13, (
        f"the derivation found suspiciously few required vars: {required} — "
        "did _require_env's call shape change?"
    )

    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = sl.render_template(BACKUP_HEALTH, ctx)
    exported = set(
        re.findall(r"^export (LONGHORN_[A-Z0-9_]+)=", rendered, re.MULTILINE)
    )

    missing = required - exported
    assert not missing, f"the shim does not export: {sorted(missing)}"


FAKE_HC_PING_KEY = "fixture-hc-ping-key"


def _run_rendered_shim(
    tmp_path,
    fake_reader_body: str,
    *,
    push_ok_unset: bool = False,
    extra_stub_files: dict[str, str] | None = None,
):
    """Run the real rendered backup-health shim with every external seam pointed at a fixture.

    The seams: the reader invocation becomes `fake_reader_body`; `kuma_push` and
    `boot_grace_active` are shadowed by shell functions; the Kuma token file and the
    healthchecks.io key file are swapped for tmp copies; and `curl` is a stub on PATH that
    records its argv. Returns `(proc, kuma_status, curl_calls)` where `kuma_status` is the
    string the `kuma_push` stub received (None when never called) and `curl_calls` is one
    argv line per curl invocation.

    `push_ok_unset` reproduces an older kuma-push-lib.sh that never set KUMA_PUSH_OK at all
    (2026-09-04 review finding #4) — the injected `kuma_push` stub omits that assignment.
    `extra_stub_files` places additional executables (name -> script body) on the same PATH
    directory as the `curl` stub, ahead of the real binaries — e.g. a `mktemp` that fails.

    The healthchecks.io seam is not optional on this host: `/etc/healthchecks/ping.env` is
    0640 root:ubuntu, so the test user CAN read the real key, and until this seam existed a
    test run sourced it and sent a fixture `up` ping to the real off-site dead-man for both
    `longhorn-backup-health` and `uptime-kuma-alive`. Two independent guards: the path is
    replaced, and `curl` is stubbed so a missed replacement leaks a real key into a file
    under tmp_path rather than onto the wire. The assertions on `curl_calls` never print a
    URL for that reason.
    """
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **v.SHELL_STUB_OVERRIDES}
    rendered = sl.render_template(BACKUP_HEALTH, ctx)

    fake_reader = tmp_path / "fake-reader.sh"
    fake_reader.write_text("#!/usr/bin/env bash\n" + fake_reader_body)
    fake_reader.chmod(0o755)

    push_token_env = tmp_path / "kuma-push.env"
    push_token_env.write_text("LONGHORN_BACKUP_PUSH_TOKEN='test-token'\n")
    hc_ping_env = tmp_path / "ping.env"
    hc_ping_env.write_text(f"HC_PING_KEY='{FAKE_HC_PING_KEY}'\n")
    kuma_push_call = tmp_path / "kuma-push-call.txt"

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl_calls = stub_bin / "curl-calls"
    curl_calls.touch()
    curl = stub_bin / "curl"
    curl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {curl_calls}\n")
    curl.chmod(0o755)
    for name, body in (extra_stub_files or {}).items():
        stub = stub_bin / name
        stub.write_text(body)
        stub.chmod(0o755)

    script = rendered
    script = script.replace(
        "source /usr/local/lib/kuma-push-lib.sh ||", f"source {KUMA_PUSH_LIB} ||"
    )
    script = script.replace("/etc/rancher/k3s/kuma-push.env", str(push_token_env))
    script = script.replace("/etc/healthchecks/ping.env", str(hc_ping_env))
    reader_invocation = re.search(
        r"/usr/local/bin/uv run --no-project --no-python-downloads --python \S+ "
        r"/opt/longhorn-backup-health/longhorn_backup_health\.py",
        script,
    )
    assert reader_invocation, "the reader invocation line moved; update this test"
    script = script.replace(reader_invocation.group(0), str(fake_reader))
    # boot_grace_active/kuma_push are shell FUNCTIONS bash resolves at call time, so defining our
    # own here — after the real `source` above, before either is actually called further down —
    # shadows the sourced versions for the rest of this run without needing to fake the library.
    push_ok_assignment = "" if push_ok_unset else "KUMA_PUSH_OK=1;"
    script = script.replace(
        "if boot_grace_active ",
        f"boot_grace_active() {{ return 1; }}\n"
        # KUMA_PUSH_OK is set by the real kuma_push and read further down by the healthchecks
        # block (`(( ${{KUMA_PUSH_OK:-1}} ))` under `set -u`) — `push_ok_unset` reproduces an
        # older lib that never made that assignment at all.
        f'kuma_push() {{ printf "%s" "$1" > {kuma_push_call}; {push_ok_assignment} }}\n'
        "if boot_grace_active ",
        1,
    )

    script_path = tmp_path / "longhorn-backup-health.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    kuma_status = kuma_push_call.read_text() if kuma_push_call.exists() else None
    return proc, kuma_status, curl_calls.read_text().splitlines()


def _assert_pings_carry_only_the_fixture_key(curl_calls: list[str]) -> None:
    # Deliberately no URL in the failure message: a missed ping.env replacement means these
    # lines hold the REAL healthchecks key.
    assert len(curl_calls) == 2, f"expected both hc-ping calls, got {len(curl_calls)}"
    assert all(FAKE_HC_PING_KEY in call for call in curl_calls), (
        "a healthchecks ping did not carry the fixture key: the ping.env path replacement "
        "in _run_rendered_shim missed, and the real key was sourced"
    )


def test_backup_health_kubectl_stderr_does_not_contaminate_the_status(
    tmp_path, logger_calls
):
    """Regression for the 2026-09-04 review's finding #1, run against the ACTUAL shipped shim.

    Until this fix, `OUT=$(... 2>&1)` meant any stderr byte the reader's own `logger` subprocess
    wrote — `logger: socket /dev/log: ...`, e.g. — landed ahead of the reader's real stdout line
    once the two streams were merged, silently turning `up` into garbage that Kuma reads as DOWN
    and pings healthchecks.io `/fail` on a backup plane that was fine. This patches in a fake
    reader that writes junk to stderr before printing `up<TAB>ok` and asserts on the status the
    `kuma_push` stub actually receives — proving the fix at the point that matters (what reaches
    Kuma) rather than just that the fix's source text exists.

    On this path the shim now DOES call `logger` — the 2026-09-04 review's finding #2. Only the
    failure branch used to call it, so this exact stderr (a green run with something on stderr)
    used to be silently dropped; see test_backup_health_logs_stderr_on_a_successful_run right
    below for that half in isolation.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'logger: socket /dev/log: No such file or directory\\n' >&2\n"
        "printf 'up\\tbackup target(s) default available, 1 backed-up volume(s) covered\\n'\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status is not None, "kuma_push was never called"
    assert kuma_status == "up", (
        f"stderr contamination reached STATUS: got {kuma_status!r}, "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    lines = logger_calls.read_text().splitlines()
    assert any("No such file or directory" in line for line in lines), lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


def test_backup_health_logs_stderr_on_a_successful_run(tmp_path, logger_calls):
    """Delta 2 (2026-09-04 review): a clean run's stderr must reach the local trail too.

    Before this fix only the `if [[ $RC -ne 0 ...` branch called `logger` — a kubectl RBAC
    warning or a uv resolution warning on an otherwise-green tick was captured into ERR and then
    silently discarded, since the success branch never read it. journalctl showed nothing for
    the one case where "the run succeeded, but something on stderr is worth knowing" is exactly
    the signal a warning exists to carry.
    """
    proc, kuma_status, _curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'uv: warning: pin resolution took a fallback path\\n' >&2\n"
        "printf 'up\\tall clean\\n'\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    lines = logger_calls.read_text().splitlines()
    assert any("pin resolution took a fallback path" in line for line in lines), lines


def test_backup_health_logs_nothing_on_a_clean_run_with_empty_stderr(
    tmp_path, logger_calls
):
    """The clean half of the pair above: no stderr, no logger call — the guard must not fire on
    nothing, which would otherwise mask the one case (an actual failure) it exists to explain.
    """
    proc, kuma_status, _curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    assert logger_calls.read_text() == ""


def test_backup_health_reader_failure_is_logged_through_the_stub(
    tmp_path, logger_calls
):
    """A reader that exits nonzero is pushed DOWN and logged, and the log hits the stub.

    The end-to-end half of `test_backup_health_logs_unconditionally_even_when_the_reader_itself_
    breaks`, which only asserts the `logger` call's source text sits in the right branch. It is
    also the non-vacuity proof for this directory's `_no_syslog` fixture: the shim's `logger`
    is the one call on any tested path, so an empty `logger_calls` here would mean the stub is
    no longer first on PATH and the real syslog took it (issue #1052).
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'Traceback: the reader broke\\n' >&2\nexit 1\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert lines == [
        "-t longhorn-backup-health status=down longhorn backup health reader failed: "
        "Traceback: the reader broke"
    ], lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


# ── delta 1 (2026-09-04 review): STATUS must be Kuma's own "up"/"down" vocabulary ───────────


def test_backup_health_a_recognized_down_status_reaches_kuma_unchanged(
    tmp_path, logger_calls
):
    """The clean half: a real DOWN from the reader must reach Kuma verbatim."""
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'down\\tbackup target unavailable\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    _assert_pings_carry_only_the_fixture_key(curl_calls)


def test_backup_health_a_tabless_last_line_is_flagged_not_pushed_as_is(
    tmp_path, logger_calls
):
    """The flagged half. A stray final stdout line with no tab used to make STATUS the whole
    line — neither "up" nor "down" — and get pushed to Kuma as-is; `[[ "$STATUS" == "up" ]]`
    then read false and pinged healthchecks.io `/fail` on a status string Kuma never defined.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'a stray line with no tab at all\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert any("unrecognized status" in line for line in lines), lines
    # And it must page — the whole point of catching this rather than pushing it as-is.
    assert any("/fail" in call for call in curl_calls), curl_calls


# ── delta 3 (2026-09-04 review): an unchecked mktemp must not go unexplained ────────────────


def test_backup_health_a_failing_mktemp_is_named_in_the_pushed_message(
    tmp_path, logger_calls
):
    """A full or read-only /tmp must be reported as ITSELF, not as an opaque reader failure with
    no clue why. `mktemp` is stubbed to fail — the same PATH seam `curl` already uses.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path,
        "printf 'up\\tall clean\\n'\n",
        extra_stub_files={"mktemp": "#!/bin/sh\nexit 1\n"},
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "down"
    lines = logger_calls.read_text().splitlines()
    assert any("mktemp" in line for line in lines), lines
    _assert_pings_carry_only_the_fixture_key(curl_calls)


# ── delta 4 (2026-09-04 review): KUMA_PUSH_OK must not be a bare reference under `set -u` ───


def test_backup_health_kuma_alive_ping_has_no_fail_suffix_when_the_push_succeeded(
    tmp_path, logger_calls
):
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    alive_calls = [c for c in curl_calls if "uptime-kuma-alive" in c]
    assert len(alive_calls) == 1
    assert "/fail" not in alive_calls[0]


def test_backup_health_an_older_lib_that_never_sets_kuma_push_ok_does_not_abort(
    tmp_path, logger_calls
):
    """The regression this delta exists for: under `set -u`, a bare `(( KUMA_PUSH_OK ))`
    reference is fatal the instant kuma-push-lib.sh doesn't set it — an older lib on the host,
    predating this var. That used to kill the script before the hc-ping call even ran, silencing
    the off-site deadman rather than reddening it. `${KUMA_PUSH_OK:-1}` must let the script keep
    running (with the default-success reading, matching pi-sd-health.sh.j2's own
    `${KUMA_PUSH_OK:-1}` convention) and still reach both curl calls.
    """
    proc, kuma_status, curl_calls = _run_rendered_shim(
        tmp_path, "printf 'up\\tall clean\\n'\n", push_ok_unset=True
    )
    assert proc.returncode == 0, proc.stderr
    assert kuma_status == "up"
    assert "unbound variable" not in proc.stderr
    assert any("longhorn-backup-health" in c for c in curl_calls)
    assert any("uptime-kuma-alive" in c for c in curl_calls)
