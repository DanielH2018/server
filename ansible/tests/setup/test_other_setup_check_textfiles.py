"""The B4 producers write a homelab_check_* textfile beside their Kuma push (M6 batch B4).

docs/target-architecture/monitoring/m6-retire-kuma.md, batch B4: the UPS secondary watchdog, the
three fake-remux heartbeats, the etcd restore drill with its egress-fence leg, and the renovate
notifier and agent each write `<check>.prom` through homelab-check-lib.sh, so the rules in
`other-setup-checks.yaml` can shadow the Kuma tiles. Each case runs the rendered script, or the
rendered function or unit line that writes the verdict, against the REAL library from the role
tree: a CI runner has no /usr/local/lib/homelab-check-lib.sh, and daniel-box does, so a case
that ran the script as-is would pass here and fail there. The Kuma library, the env files, `uv`,
`upsc`, `systemctl`, `date` and `logger` are stubs.

Properties the cases pin:

- the file carries success 1 for an up verdict and 0 for a down one, the stamped time and the
  rendered deadline;
- the file exists before the Kuma push is attempted, because a push retries for over a minute;
- a missing push token never costs the textfile.

The rule literals are pinned to the rendered deadlines in test_b4_rule_literals.py.

Run: uv run pytest ansible/tests/setup/test_other_setup_check_textfiles.py
"""

import re
import shlex

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, path_with, run, write_exec

CHECK_LIB = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
)
CHECK_LIB_PATH = "/usr/local/lib/homelab-check-lib.sh"
PUSH_LIB_PATH = "/usr/local/lib/kuma-push-lib.sh"
EPOCH = 1760000000
OVERRIDES = {
    "domain": "example.test",
    "k3s_metallb_ingress_vip": "10.0.0.240",
    "sys_user": "ubuntu",
}
# Records the status, and whether the textfile of the verdict's check already existed.
PUSH_STUB = """\
kuma_push() {
  printf '%s %s\\n' "$1" "$([ -f "$TEXTFILE_DIR/$CHECK_UNDER_TEST.prom" ] && echo after-textfile || echo BEFORE-TEXTFILE)" \\
    >> "$PUSHED"
}
"""


def _series(textfile_dir, check) -> dict[str, str]:
    """`name{labels}` -> value for every sample line in `<check>.prom`."""
    lines = (textfile_dir / f"{check}.prom").read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


def _value(textfile_dir, check, metric) -> str:
    return _series(textfile_dir, check)[f'{metric}{{check="{check}"}}']


def _pushes(tmp_path) -> list[str]:
    pushed = tmp_path / "pushed"
    return pushed.read_text().splitlines() if pushed.exists() else []


def _env(tmp_path, check, stubs=None):
    """(textfile dir, the environment and stub directory a case runs under)."""
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    bin_dir = fake_bin(
        tmp_path / "bin",
        date=f"echo {EPOCH}\n",
        logger='echo "$@" >> "$LOGGED"\n',
        **(stubs or {}),
    )
    env = {
        "TEXTFILE_DIR": str(textfile_dir),
        "LOGGED": str(tmp_path / "logged"),
        "PUSHED": str(tmp_path / "pushed"),
        "CHECK_UNDER_TEST": check,
        "PATH": path_with(bin_dir),
    }
    return textfile_dir, env


def _push_lib(tmp_path):
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(PUSH_STUB)
    return lib


# --- ups-secondary-health.sh -----------------------------------------------------------------


def _run_ups(tmp_path, unit_state="active", token="tok"):
    body = render_shell_script(
        "setup", "nut_host", "ups-secondary-health.sh.j2", OVERRIDES
    )
    upsmon, env_file = tmp_path / "upsmon.conf", tmp_path / "kuma-push.env"
    upsmon.write_text("MONITOR ups@10.43.0.9 1 user pw secondary\n")
    env_file.write_text(f'UPS_SECONDARY_PUSH_TOKEN="{token}"\n')
    for literal in (
        PUSH_LIB_PATH,
        CHECK_LIB_PATH,
        "/etc/nut/upsmon.conf",
        "/etc/nut/kuma-push.env",
    ):
        assert literal in body, (
            f"ups-secondary-health.sh no longer references {literal}"
        )
    body = (
        body.replace(PUSH_LIB_PATH, str(_push_lib(tmp_path)))
        .replace(CHECK_LIB_PATH, str(CHECK_LIB))
        .replace("/etc/nut/upsmon.conf", str(upsmon))
        .replace("/etc/nut/kuma-push.env", str(env_file))
    )
    textfile_dir, env = _env(
        tmp_path,
        "ups-secondary-health",
        stubs={"systemctl": f"echo {unit_state}\n", "upsc": "echo OL\n"},
    )
    run(["bash", str(write_exec(tmp_path / "ups.sh", body))], env=env, check=True)
    return textfile_dir


def test_an_up_ups_verdict_is_written_with_the_deadline_before_the_push(tmp_path):
    textfile_dir = _run_ups(tmp_path)

    check = "ups-secondary-health"
    assert _value(textfile_dir, check, "homelab_check_success") == "1"
    assert _value(
        textfile_dir, check, "homelab_check_last_run_timestamp_seconds"
    ) == str(EPOCH)
    # nut_host_watchdog_interval_minutes (10) x 2 periods + 300.
    assert _value(textfile_dir, check, "homelab_check_deadline_seconds") == "1500"
    assert _pushes(tmp_path) == ["up after-textfile"]


def test_a_down_ups_verdict_writes_zero_and_a_missing_token_still_writes(tmp_path):
    textfile_dir = _run_ups(tmp_path, unit_state="inactive", token="")

    assert _value(textfile_dir, "ups-secondary-health", "homelab_check_success") == "0"
    assert _pushes(tmp_path) == []


# --- fake-remux-health.sh --------------------------------------------------------------------

FAKE_REMUX_CHECKS = ["fake-remux-scan", "fake-remux-replace", "mkv-attachment-repair"]
# One line per state file, in argument order: scan up, replace down, mkv repair up.
STATE_PUSH = "printf 'up\\tscan ok\\ndown\\treplace failed\\nup\\tmkv ok\\n'\n"


def _run_fake_remux(tmp_path, tokens=True):
    body = render_shell_script(
        "setup", "fake_remux", "fake-remux-health.sh.j2", OVERRIDES
    )
    token_env = tmp_path / "kuma-push.env"
    token_env.write_text(
        'FAKE_REMUX_SCAN_PUSH_TOKEN="a"\nFAKE_REMUX_REPLACE_PUSH_TOKEN="b"\nMKV_ATTACHMENT_PUSH_TOKEN="c"\n'
        if tokens
        else ""
    )
    uv = tmp_path / "uv"
    write_exec(uv, STATE_PUSH)
    # kuma_push gets the tile's textfile through its status, so give the stub the loop's order.
    push_lib = tmp_path / "kuma-push-lib.sh"
    push_lib.write_text(
        'kuma_push() {\n  local check="${CHECKS_IN_ORDER[$PUSH_N]}"; PUSH_N=$((PUSH_N + 1))\n'
        '  printf \'%s %s\\n\' "$1" "$([ -f "$TEXTFILE_DIR/${check}.prom" ] && echo after-textfile '
        '|| echo BEFORE-TEXTFILE)" >> "$PUSHED"\n}\n'
        "PUSH_N=0\nCHECKS_IN_ORDER=(fake-remux-scan fake-remux-replace mkv-attachment-repair)\n"
    )
    for literal in (
        PUSH_LIB_PATH,
        CHECK_LIB_PATH,
        "/etc/autofix-fake-remux/kuma-push.env",
        "/usr/local/bin/uv",
    ):
        assert literal in body, f"fake-remux-health.sh no longer references {literal}"
    body = (
        body.replace(PUSH_LIB_PATH, str(push_lib))
        .replace(CHECK_LIB_PATH, str(CHECK_LIB))
        .replace("/etc/autofix-fake-remux/kuma-push.env", str(token_env))
        .replace("/usr/local/bin/uv", str(uv))
    )
    textfile_dir, env = _env(tmp_path, "unused")
    run(["bash", str(write_exec(tmp_path / "fr.sh", body))], env=env, check=True)
    return textfile_dir


def test_each_fake_remux_state_file_gets_its_own_verdict_before_its_push(tmp_path):
    textfile_dir = _run_fake_remux(tmp_path)

    verdicts = {
        check: _value(textfile_dir, check, "homelab_check_success")
        for check in FAKE_REMUX_CHECKS
    }
    assert verdicts == {
        "fake-remux-scan": "1",
        "fake-remux-replace": "0",
        "mkv-attachment-repair": "1",
    }
    for check in FAKE_REMUX_CHECKS:
        # fake_remux_health_cron_minute (*/10) x 2 periods + 300.
        assert _value(textfile_dir, check, "homelab_check_deadline_seconds") == "1500"
    assert _pushes(tmp_path) == [
        "up after-textfile",
        "down after-textfile",
        "up after-textfile",
    ]


def test_unreadable_fake_remux_tokens_skip_the_pushes_and_not_the_textfiles(tmp_path):
    textfile_dir = _run_fake_remux(tmp_path, tokens=False)

    for check in FAKE_REMUX_CHECKS:
        assert (textfile_dir / f"{check}.prom").is_file()
    assert _pushes(tmp_path) == []


# --- etcd-restore-drill-vm.sh: the two report functions --------------------------------------


def _run_drill_reports(tmp_path):
    """Call the rendered `report` and `report_fence` functions, lifted out of the orchestrator."""
    body = render_shell_script(
        "setup", "hypervisor", "etcd-restore-drill-vm.sh.j2", OVERRIDES
    )
    lifted = []
    for name in ("write_verdict", "report", "report_fence"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", body, re.M | re.S)
        assert match, (
            f"{name}() is gone from etcd-restore-drill-vm.sh.j2; this module lifts it"
        )
        lifted.append(match.group(0))
    constants = []
    for name in ("CHECK_DRILL", "CHECK_FENCE", "DEADLINE_S", "TAG"):
        [constant] = re.findall(rf"^{name}=.*$", body, re.M)
        constants.append(constant)
    textfile_dir, env = _env(tmp_path, "unused")
    driver = (
        "set -uo pipefail\n"
        f"source {CHECK_LIB}\n"
        + "\n".join(constants)
        + '\nRUN_ID=r1\nFENCE_PUSH_TOKEN=""\nlog() { :; }\n'
        # push_kuma records the status and which textfiles exist when the push starts.
        + 'push_kuma() { printf "%s %s\\n" "$1" "$(cd "$TEXTFILE_DIR" && echo *.prom)" >> "$PUSHED"; }\n'
        + "\n".join(lifted)
        + "\nreport down 'drill failed'\nreport_fence up 'fence holds'\n"
    )
    run(["bash", str(write_exec(tmp_path / "drill.sh", driver))], env=env, check=True)
    return textfile_dir


def test_the_drill_and_its_fence_leg_write_separate_verdicts(tmp_path):
    textfile_dir = _run_drill_reports(tmp_path)

    # Each push finds its own verdict already on disk.
    assert _pushes(tmp_path) == [
        "down etcd-restore-drill-vm.prom",
        "up etcd-restore-drill-egress-fence.prom etcd-restore-drill-vm.prom",
    ]
    assert _value(textfile_dir, "etcd-restore-drill-vm", "homelab_check_success") == "0"
    assert (
        _value(textfile_dir, "etcd-restore-drill-egress-fence", "homelab_check_success")
        == "1"
    )
    for check in ("etcd-restore-drill-vm", "etcd-restore-drill-egress-fence"):
        # etcd_drill_full_kuma_interval_s: the monthly cadence plus four days of slack.
        assert (
            _value(textfile_dir, check, "homelab_check_deadline_seconds") == "3024000"
        )
        assert _value(
            textfile_dir, check, "homelab_check_last_run_timestamp_seconds"
        ) == str(EPOCH)


# --- the renovate units' ExecStartPost -------------------------------------------------------

UNITS = {
    "renovate-notify": ("renovate_notify", "renovate-notify.service.j2", {}),
    "renovate-agent": (
        "renovate_agent",
        "renovate-agent.service.j2",
        {"renovate_agent_kuma_push_token": "tok"},
    ),
}


PINNED_ENV = ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin"]


def _textfile_argv(unit_text) -> list[str]:
    """The unit's textfile ExecStartPost as an argv, with the `+` and systemd's `%%` undone."""
    [line] = [
        raw
        for raw in unit_text.splitlines()
        if raw.startswith("ExecStartPost=+") and "write_check_textfile" in raw
    ]
    return shlex.split(line.removeprefix("ExecStartPost=+").replace("%%", "%"))


def _run_unit_line(tmp_path, check, lib, bin_dir_env):
    """Run the unit's textfile line with `lib` as the check library and stubs ahead of PATH."""
    role, template, overrides = UNITS[check]
    argv = _textfile_argv(render_setup_text(role, template, overrides))
    # The pin is what keeps the agent's user-writable ~/.local/bin out of a root command.
    assert argv[:3] == PINNED_ENV
    assert argv[3:5] == ["/bin/bash", "-c"]
    stubs = bin_dir_env["PATH"].split(":", 1)[0]
    # `env -i` clears the case's environment too, so the stubs and the temp paths ride as arguments.
    argv = [
        *argv[:2],
        f"PATH={stubs}:/usr/bin:/bin",
        f"TEXTFILE_DIR={bin_dir_env['TEXTFILE_DIR']}",
        f"LOGGED={bin_dir_env['LOGGED']}",
        *argv[3:-1],
        argv[-1].replace(CHECK_LIB_PATH, str(lib)),
    ]
    return run(argv, env=bin_dir_env)


@pytest.mark.parametrize("check", sorted(UNITS))
def test_a_renovate_unit_writes_success_one_with_its_tiles_deadline(tmp_path, check):
    textfile_dir, env = _env(tmp_path, check)

    done = _run_unit_line(tmp_path, check, CHECK_LIB, env)

    assert done.returncode == 0
    assert _value(textfile_dir, check, "homelab_check_success") == "1"
    assert _value(
        textfile_dir, check, "homelab_check_last_run_timestamp_seconds"
    ) == str(EPOCH)
    assert _value(textfile_dir, check, "homelab_check_deadline_seconds") == (
        "129600" if check == "renovate-notify" else "100800"
    )


@pytest.mark.parametrize("check", sorted(UNITS))
def test_a_missing_library_logs_and_exits_zero_so_the_unit_never_fails_over_telemetry(
    tmp_path, check
):
    textfile_dir, env = _env(tmp_path, check)

    done = _run_unit_line(tmp_path, check, tmp_path / "absent.sh", env)

    assert done.returncode == 0
    assert "textfile write failed" in (tmp_path / "logged").read_text()
    assert not list(textfile_dir.iterdir())


def test_the_agent_writes_its_textfile_before_the_kuma_beat_can_fail_the_unit():
    unit = render_setup_text(
        "renovate_agent", "renovate-agent.service.j2", UNITS["renovate-agent"][2]
    )
    posts = [line for line in unit.splitlines() if line.startswith("ExecStartPost=")]

    assert len(posts) == 2
    assert "write_check_textfile" in posts[0] and "curl" in posts[1]
