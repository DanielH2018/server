"""The initial_setup crons write a homelab_check_* textfile beside their Kuma push (M6 batch B2).

docs/target-architecture/monitoring/m6-retire-kuma.md, batch B2: docs-refresh, the eval sweep, the
secret-rotation audit and the setup-drift check each write `<check>.prom` through
homelab-check-lib.sh before they push, so the rules in `initial-setup-checks.yaml` can shadow the
Kuma tiles. (The Loki witness's cases are in test_loki_route_witness.py.) Each case runs the
rendered script, or the rendered `on_exit` trap for the two scripts too large to run whole, with
the real library, a `date` stub and the push library stubbed to record what it was handed.

Three properties each case pins:

- the file carries success 1 for an up verdict and 0 for a down one, and the stamped time;
- the file exists before the Kuma push is attempted, because a push retries for over a minute;
- a missing push token or a lost push never costs the textfile.

Run: uv run pytest ansible/tests/setup/test_initial_setup_check_textfiles.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, path_with, run, write_exec

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
CHECK_LIB = ROLE / "files" / "homelab-check-lib.sh"
PUSH_LIB = "/usr/local/lib/kuma-push-lib.sh"
CHECK_LIB_PATH = "/usr/local/lib/homelab-check-lib.sh"
EPOCH = 1760000000
OVERRIDES = {
    "domain": "example.test",
    "k3s_metallb_ingress_vip": "10.0.0.240",
    "sys_user": "ubuntu",
}


def _render(template: str) -> str:
    return render_shell_script("setup", "initial_setup", template, overrides=OVERRIDES)


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
        "PUSHED": str(tmp_path / "pushed"),
        "PATH": path_with(bin_dir),
        **extra,
    }
    return textfile_dir, env


PUSH_STUB = """\
kuma_push() {
  # Records the status, and whether the textfile already existed when the push began.
  printf '%s %s\\n' "$1" "$([ -f "$TEXTFILE_DIR/$KUMA_TAG.prom" ] && echo after-textfile || echo BEFORE-TEXTFILE)" \\
    >> "$PUSHED"
}
"""


def _pushes(tmp_path) -> list[str]:
    pushed = tmp_path / "pushed"
    return pushed.read_text().splitlines() if pushed.exists() else []


# --- docs-refresh and eval-run: the on_exit trap, lifted out of the render -----------------


def _on_exit(script: str, name: str) -> tuple[str, int]:
    """(the trap's body, the DEADLINE_S the script renders) for a rendered script."""
    match = re.search(r"^on_exit\(\) \{\n.*?^\}$", script, re.M | re.S)
    assert match, f"on_exit() is gone from {name}; this module lifts it by name"
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", script, re.M)
    return match.group(0), int(deadline)


TRAP_SCRIPTS = {
    "docs-refresh": ("docs-refresh.sh.j2", "DOCS_REFRESH_PUSH_TOKEN"),
    "eval-run": ("eval-run.sh.j2", "EVAL_PUSH_TOKEN"),
}


def _run_trap(tmp_path, check, status, token="tok"):
    template, token_var = TRAP_SCRIPTS[check]
    body, deadline = _on_exit(_render(template), template)
    textfile_dir, env = _env(tmp_path, KUMA_TAG=check)
    driver = tmp_path / "driver.sh"
    write_exec(
        driver,
        "set -uo pipefail\n"
        f"source {CHECK_LIB}\n"
        f"{PUSH_STUB}"
        f"{body}\n"
        f"DEADLINE_S={deadline}\n"
        f'{token_var}="{token}"\n'
        'PUSH_URL=u KUMA_HOST=h KUMA_RESOLVE_IP=i PUSH_MSG="a message"\n'
        f"PUSH_STATUS={status}\n"
        "on_exit\n",
    )
    run(["bash", str(driver)], env=env, check=True)
    return textfile_dir, deadline


@pytest.mark.parametrize("check", sorted(TRAP_SCRIPTS))
def test_an_up_exit_writes_success_one_with_the_deadline_before_the_push(
    tmp_path, check
):
    textfile_dir, deadline = _run_trap(tmp_path, check, "up")

    series = _series(textfile_dir, check)
    assert series[f'homelab_check_success{{check="{check}"}}'] == "1"
    assert series[
        f'homelab_check_last_run_timestamp_seconds{{check="{check}"}}'
    ] == str(EPOCH)
    assert series[f'homelab_check_deadline_seconds{{check="{check}"}}'] == str(deadline)
    assert _pushes(tmp_path) == ["up after-textfile"]


@pytest.mark.parametrize("check", sorted(TRAP_SCRIPTS))
def test_a_down_exit_writes_success_zero_and_a_missing_token_still_writes(
    tmp_path, check
):
    textfile_dir, _ = _run_trap(tmp_path, check, "down", token="")

    assert (
        _series(textfile_dir, check)[f'homelab_check_success{{check="{check}"}}'] == "0"
    )
    assert _pushes(tmp_path) == []


# --- setup-drift-check: the whole script, with its two libraries stubbed ----------------------


def _run_setup_drift(tmp_path, drifted="", token="tok"):
    body = _render("setup-drift-check.sh.j2")
    env_file = tmp_path / "push.env"
    env_file.write_text(f"SETUP_DRIFT_PUSH_TOKEN={token}\n" if token else "")
    drift_lib = tmp_path / "setup-drift-lib.sh"
    drift_lib.write_text(
        'setup_drift_scan() { DRIFTED="$STUB_DRIFTED"; STALE=""; DEPLOYED_NOTE=""; '
        'MANIFEST_NOTE=""; }\nsetup_drift_tree_age_days() { echo 1; }\n'
    )
    push_lib = tmp_path / "kuma-push-lib.sh"
    push_lib.write_text(PUSH_STUB)
    for needle in (
        PUSH_LIB,
        CHECK_LIB_PATH,
        "/usr/local/lib/setup-drift-lib.sh",
        "/etc/homelab/kuma-push.env",
    ):
        assert needle in body, f"setup-drift-check.sh no longer references {needle}"
    body = (
        body.replace(PUSH_LIB, str(push_lib))
        .replace(CHECK_LIB_PATH, str(CHECK_LIB))
        .replace("/usr/local/lib/setup-drift-lib.sh", str(drift_lib))
        .replace("/etc/homelab/kuma-push.env", str(env_file))
    )
    script = write_exec(tmp_path / "setup-drift-check.sh", body)
    textfile_dir, env = _env(
        tmp_path, KUMA_TAG="setup-drift-check", STUB_DRIFTED=drifted
    )
    result = run([str(script)], env=env)
    return textfile_dir, result.returncode


def test_setup_drift_writes_success_one_when_nothing_drifted_and_exits_zero(tmp_path):
    textfile_dir, code = _run_setup_drift(tmp_path)

    series = _series(textfile_dir, "setup-drift-check")
    assert series['homelab_check_success{check="setup-drift-check"}'] == "1"
    assert (
        series['homelab_check_deadline_seconds{check="setup-drift-check"}'] == "90000"
    )
    assert _pushes(tmp_path) == ["up after-textfile"]
    assert code == 0


def test_setup_drift_writes_success_zero_on_drift_and_exits_one_for_the_timer(tmp_path):
    textfile_dir, code = _run_setup_drift(tmp_path, drifted="ansible/x.sh")

    assert (
        _series(textfile_dir, "setup-drift-check")[
            'homelab_check_success{check="setup-drift-check"}'
        ]
        == "0"
    )
    assert _pushes(tmp_path) == ["down after-textfile"]
    assert code == 1


def test_setup_drift_without_a_push_token_still_writes_the_textfile(tmp_path):
    textfile_dir, code = _run_setup_drift(tmp_path, token="")

    assert (
        _series(textfile_dir, "setup-drift-check")[
            'homelab_check_success{check="setup-drift-check"}'
        ]
        == "1"
    )
    assert _pushes(tmp_path) == []
    assert code == 0


# --- secret-rotation-audit: the audit's exit code is the verdict ------------------------------


def _run_audit(tmp_path, audit_rc=0, dirty=False):
    """Run the rendered wrapper in a scratch checkout with `uv` standing in for the audit."""
    repo = tmp_path / "server"
    (repo / "ansible" / "vars").mkdir(parents=True)
    (repo / "ansible" / "vars" / "secrets.yml").write_text("a: 1\n")
    (repo / "ansible" / "secret_rotation.yml").write_text("b: 1\n")
    uv = write_exec(tmp_path / "uv", "#!/usr/bin/env bash\nexit $STUB_AUDIT_RC\n")
    push_lib = tmp_path / "kuma-push-lib.sh"
    push_lib.write_text(PUSH_STUB)
    body = _render("secret-rotation-audit.sh.j2")
    for needle in (
        PUSH_LIB,
        CHECK_LIB_PATH,
        "/home/ubuntu/server",
        "/home/ubuntu/.local/bin/uv",
    ):
        assert needle in body, f"secret-rotation-audit.sh no longer references {needle}"
    body = (
        # The checkout path first: the library path substituted below sits under /home/ubuntu/server.
        body.replace("/home/ubuntu/server", str(repo))
        .replace("/home/ubuntu/.local/bin/uv", str(uv))
        .replace(PUSH_LIB, str(push_lib))
        .replace(CHECK_LIB_PATH, str(CHECK_LIB))
    )
    script = write_exec(tmp_path / "secret-rotation-audit.sh", body)
    textfile_dir, env = _env(
        tmp_path, KUMA_TAG="secret-rotation-audit", STUB_AUDIT_RC=str(audit_rc)
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
    result = run([str(script)], env=env)
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


def test_a_dirty_tree_short_circuit_writes_success_zero_before_it_pushes(tmp_path):
    textfile_dir, code = _run_audit(tmp_path, dirty=True)

    assert (
        _series(textfile_dir, "secret-rotation-audit")[
            'homelab_check_success{check="secret-rotation-audit"}'
        ]
        == "0"
    )
    assert _pushes(tmp_path) == ["down after-textfile"]
    assert code == 1
