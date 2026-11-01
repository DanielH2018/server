"""The B2 host checks write a homelab_check_* textfile and push to nothing (M6 batch B2).

docs/target-architecture/monitoring/m6-retire-kuma.md, batch B2: docs-refresh, the eval sweep, the
secret-rotation audit and the setup-drift check each write `<check>.prom` through
homelab-check-lib.sh, which the rules in `initial-setup-checks.yaml` read. (The Loki witness's
cases are in test_loki_route_witness.py.) Each case runs the rendered script, or the rendered
`on_exit` trap for the two scripts too large to run whole, with the real library and a `date`
stub.

Two properties each case pins:

- the file carries success 1 for an up verdict and 0 for a down one, and the stamped time;
- since B2's Cutover-2 no render pushes to Kuma: no library, no `kuma_push`, no push URL and no
  push credential, so a run needs neither to write its verdict.

Run: uv run pytest ansible/tests/setup/test_initial_setup_check_textfiles.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, path_with, run, write_exec

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
COMPONENTS = ANSIBLE.parent / "hosts" / "components"
CHECK_LIB = ROLE / "files" / "homelab-check-lib.sh"
CHECK_LIB_PATH = "/usr/local/lib/homelab-check-lib.sh"
EPOCH = 1760000000
OVERRIDES = {
    "domain": "example.test",
    "k3s_metallb_ingress_vip": "10.0.0.240",
    "sys_user": "ubuntu",
}
# What a render carries while it still pushes to Kuma: the library, its call, a push URL, or the
# credential that fed the token.
KUMA_PUSH_MARKERS = (
    "kuma-push-lib",
    "kuma_push",
    "/api/push/",
    "_push_token",
    "PUSH_TOKEN",
)
# Every B2 shell producer this module covers, with the check its textfile names. secret-rotate
# writes no textfile, and the daily audit reports what it leaves behind.
PRODUCERS = {
    "docs-refresh.sh.j2": "docs-refresh",
    "eval-run.sh.j2": "eval-run",
    "secret-rotation-audit.sh.j2": "secret-rotation-audit",
    "secret-rotate.sh.j2": None,
    "setup-drift-check.sh.j2": "setup-drift-check",
}


def _render(template: str) -> str:
    """The script as rendered, from its host component once H7.36 moved it out of the role."""
    for path in sorted(COMPONENTS.glob(f"*/templates/{template}")):
        return render_shell_script(
            "components", path.parents[1].name, template, overrides=OVERRIDES
        )
    return render_shell_script("setup", "initial_setup", template, overrides=OVERRIDES)


def kuma_push_markers(body: str) -> list[str]:
    """The Kuma push markers a rendered script still carries, or nothing."""
    return [marker for marker in KUMA_PUSH_MARKERS if marker in body]


@pytest.mark.parametrize("template", sorted(PRODUCERS))
def test_no_b2_producer_pushes_to_kuma(template):
    # RED if a push, its library, its token or its credential returns: the tiles are gone, so a
    # push would be refused (`by=kuma`) every run.
    body = _render(template)
    check = PRODUCERS[template]
    # Non-vacuity: the render is the real script, which still records its verdict.
    if check:
        assert f"write_check_textfile {check}" in body
    else:
        assert 'logger -t secret-rotate "status=down $1"' in body
    assert kuma_push_markers(body) == []


def test_a_script_that_pushes_to_kuma_is_flagged():
    pushing = (
        "source /usr/lib/homelab/kuma-push-lib.sh\n"
        'TOKEN="$(< "$CREDENTIALS_DIRECTORY/docs_refresh_push_token")"\n'
        'DOCS_REFRESH_PUSH_TOKEN="$TOKEN"\n'
        'kuma_push "$1" "$2" "https://kuma/api/push/$TOKEN"\n'
    )
    assert kuma_push_markers(pushing) == list(KUMA_PUSH_MARKERS)


def _series(textfile_dir, check) -> dict[str, str]:
    """`name{labels}` -> value for every sample line in `<check>.prom`."""
    lines = (textfile_dir / f"{check}.prom").read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


def _env(tmp_path, **extra):
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    bin_dir = fake_bin(
        tmp_path / "bin", date=f"echo {EPOCH}\n", logger='echo "$@" >> "$LOGGED"\n'
    )
    env = {
        "TEXTFILE_DIR": str(textfile_dir),
        "LOGGED": str(tmp_path / "logged"),
        "PATH": path_with(bin_dir),
        **extra,
    }
    return textfile_dir, env


def _logged(tmp_path) -> str:
    logged = tmp_path / "logged"
    return logged.read_text() if logged.exists() else ""


# --- docs-refresh and eval-run: the on_exit trap, lifted out of the render -----------------


def _on_exit(script: str, name: str) -> tuple[str, int]:
    """(the trap's body, the DEADLINE_S the script renders) for a rendered script."""
    match = re.search(r"^on_exit\(\) \{\n.*?^\}$", script, re.M | re.S)
    assert match, f"on_exit() is gone from {name}; this module lifts it by name"
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", script, re.M)
    return match.group(0), int(deadline)


TRAP_SCRIPTS = {
    "docs-refresh": "docs-refresh.sh.j2",
    "eval-run": "eval-run.sh.j2",
}


def _run_trap(tmp_path, check, status):
    template = TRAP_SCRIPTS[check]
    body, deadline = _on_exit(_render(template), template)
    textfile_dir, env = _env(tmp_path)
    driver = tmp_path / "driver.sh"
    write_exec(
        driver,
        "set -uo pipefail\n"
        f"source {CHECK_LIB}\n"
        f"{body}\n"
        f"DEADLINE_S={deadline}\n"
        'PUSH_MSG="a message"\n'
        f"PUSH_STATUS={status}\n"
        "on_exit\n",
    )
    run(["bash", str(driver)], env=env, check=True)
    return textfile_dir, deadline


@pytest.mark.parametrize("check", sorted(TRAP_SCRIPTS))
def test_an_up_exit_writes_success_one_with_the_deadline(tmp_path, check):
    textfile_dir, deadline = _run_trap(tmp_path, check, "up")

    series = _series(textfile_dir, check)
    assert series[f'homelab_check_success{{check="{check}"}}'] == "1"
    assert series[
        f'homelab_check_last_run_timestamp_seconds{{check="{check}"}}'
    ] == str(EPOCH)
    assert series[f'homelab_check_deadline_seconds{{check="{check}"}}'] == str(deadline)
    assert "status=up a message" in _logged(tmp_path)


@pytest.mark.parametrize("check", sorted(TRAP_SCRIPTS))
def test_a_down_exit_writes_success_zero_and_logs_its_status_line(tmp_path, check):
    textfile_dir, _ = _run_trap(tmp_path, check, "down")

    assert (
        _series(textfile_dir, check)[f'homelab_check_success{{check="{check}"}}'] == "0"
    )
    assert "status=down a message" in _logged(tmp_path)


# --- setup-drift-check: the whole script, with the drift library stubbed ----------------------


def _run_setup_drift(tmp_path, drifted=""):
    body = _render("setup-drift-check.sh.j2")
    drift_lib = tmp_path / "setup-drift-lib.sh"
    drift_lib.write_text(
        'setup_drift_scan() { DRIFTED="$STUB_DRIFTED"; STALE=""; DEPLOYED_NOTE=""; '
        'MANIFEST_NOTE=""; }\nsetup_drift_tree_age_days() { echo 1; }\n'
    )
    for needle in (CHECK_LIB_PATH, "/usr/local/lib/setup-drift-lib.sh"):
        assert needle in body, f"setup-drift-check.sh no longer references {needle}"
    body = body.replace(CHECK_LIB_PATH, str(CHECK_LIB)).replace(
        "/usr/local/lib/setup-drift-lib.sh", str(drift_lib)
    )
    script = write_exec(tmp_path / "setup-drift-check.sh", body)
    textfile_dir, env = _env(tmp_path, STUB_DRIFTED=drifted)
    result = run([str(script)], env=env)
    return textfile_dir, result.returncode


def test_setup_drift_writes_success_one_when_nothing_drifted_and_exits_zero(tmp_path):
    textfile_dir, code = _run_setup_drift(tmp_path)

    series = _series(textfile_dir, "setup-drift-check")
    assert series['homelab_check_success{check="setup-drift-check"}'] == "1"
    assert (
        series['homelab_check_deadline_seconds{check="setup-drift-check"}'] == "90000"
    )
    assert code == 0


def test_setup_drift_writes_success_zero_on_drift_and_exits_one_for_the_timer(tmp_path):
    textfile_dir, code = _run_setup_drift(tmp_path, drifted="ansible/x.sh")

    assert (
        _series(textfile_dir, "setup-drift-check")[
            'homelab_check_success{check="setup-drift-check"}'
        ]
        == "0"
    )
    assert "status=down" in _logged(tmp_path)
    assert code == 1


# --- secret-rotation-audit: the audit's exit code is the verdict ------------------------------


def _run_audit(tmp_path, audit_rc=0, dirty=False):
    """Run the rendered wrapper in a scratch checkout with `uv` standing in for the audit.

    The stub records its argv, so a case can read which flag the wrapper asked for.
    """
    repo = tmp_path / "server"
    (repo / "ansible" / "vars").mkdir(parents=True)
    (repo / "ansible" / "vars" / "secrets.yml").write_text("a: 1\n")
    (repo / "ansible" / "secret_rotation.yml").write_text("b: 1\n")
    uv = write_exec(
        tmp_path / "uv",
        '#!/usr/bin/env bash\necho "$*" >> "$UV_ARGV"\nexit $STUB_AUDIT_RC\n',
    )
    body = _render("secret-rotation-audit.sh.j2")
    # The packaged script (H7.36) sources the check library from /usr/lib/homelab.
    audit_check_lib = "/usr/lib/homelab/homelab-check-lib.sh"
    for needle in (
        audit_check_lib,
        "/home/ubuntu/server",
        "/home/ubuntu/.local/bin/uv",
    ):
        assert needle in body, f"secret-rotation-audit no longer references {needle}"
    body = (
        # The checkout path first: the library path substituted below sits under /home/ubuntu/server.
        body.replace("/home/ubuntu/server", str(repo))
        .replace("/home/ubuntu/.local/bin/uv", str(uv))
        .replace(audit_check_lib, str(CHECK_LIB))
    )
    script = write_exec(tmp_path / "secret-rotation-audit.sh", body)
    textfile_dir, env = _env(
        tmp_path, STUB_AUDIT_RC=str(audit_rc), UV_ARGV=str(tmp_path / "uv-argv")
    )
    # `gh` answers the token check, and `git` reports a dirty tree or a clean one.
    stubs = fake_bin(
        tmp_path / "bin",
        gh="exit 0\n",
        git=(
            'case "$1" in\n'
            '  status) echo "$STUB_GIT_STATUS" ;;\n'
            "  rev-list) echo 0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        ),
    )
    env["PATH"] = path_with(stubs)
    env["STUB_GIT_STATUS"] = " M ansible/vars/secrets.yml" if dirty else ""
    result = run([str(script)], env=env, check=False)
    return textfile_dir, result.returncode


def test_a_clean_audit_writes_success_one_and_exits_zero(tmp_path):
    textfile_dir, code = _run_audit(tmp_path, audit_rc=0)

    series = _series(textfile_dir, "secret-rotation-audit")
    assert series['homelab_check_success{check="secret-rotation-audit"}'] == "1"
    assert (
        series['homelab_check_deadline_seconds{check="secret-rotation-audit"}']
        == "90000"
    )
    assert code == 0
    # The audit is asked for its verdict, which no longer means a push.
    assert "secret_rotation.py audit --verdict" in (tmp_path / "uv-argv").read_text()


def test_a_failed_audit_writes_success_zero_and_keeps_its_exit_code_for_the_timer(
    tmp_path,
):
    textfile_dir, code = _run_audit(tmp_path, audit_rc=1)

    assert (
        _series(textfile_dir, "secret-rotation-audit")[
            'homelab_check_success{check="secret-rotation-audit"}'
        ]
        == "0"
    )
    assert code == 1


def test_a_dirty_tree_short_circuit_writes_success_zero_and_logs_its_reason(tmp_path):
    textfile_dir, code = _run_audit(tmp_path, dirty=True)

    assert (
        _series(textfile_dir, "secret-rotation-audit")[
            'homelab_check_success{check="secret-rotation-audit"}'
        ]
        == "0"
    )
    assert "status=down secret-rotate left uncommitted changes" in _logged(tmp_path)
    assert code == 1
