"""`kuma_push STATUS MSG TOKEN TAG` and `hc_ping SLUG STATUS TAG [BODY]` (kuma-push-lib.sh).

The library is rendered with the Kuma host and the ingress VIP baked in, so a caller passes only
its token (#4219). The six-argument form stays for the deploy window in which a host runs a cron
rendered before that change against the new library. `hc_ping` is the one healthchecks.io
sender every host cron calls (#4220).

Every test sources the RENDERED library, because the baked host and VIP are the point: the raw
template holds `{{ domain }}` where the host should be. The stub curl records its argv and the
config it read on stdin, so each assertion is on what would have gone over the wire.
"""

import pytest
from _shell_render import render_shell_script
from lib.proc_testing import run

PING_ENV = "/etc/healthchecks/ping.env"
# The fixture domain and the VIP the render context resolves, asserted by
# `test_the_render_bakes_the_fleet_constants` so the expectations below cannot drift from it.
VIP = "10.0.0.240"


@pytest.fixture(scope="module")
def rendered_lib():
    return render_shell_script("setup", "initial_setup", "kuma-push-lib.sh.j2")


def _call(tmp_path, lib_text, call, ping_env_text=None):
    """Source `lib_text` with curl, logger and sleep stubbed, run `call`, and return the record.

    The ping env path is rewritten to a file under `tmp_path`, the same way
    `test_backup_health_shim.py` points a script at a test copy of the library.
    """
    ping_env = tmp_path / "ping.env"
    if ping_env_text is not None:
        ping_env.write_text(ping_env_text)
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(lib_text.replace(PING_ENV, str(ping_env)))
    argv, stdin, logs = tmp_path / "argv", tmp_path / "stdin", tmp_path / "logs"
    for f in (argv, stdin, logs):
        f.write_text("")
    script = f"""
    set -uo pipefail
    source {lib}
    curl() {{ printf '%s\\n' "$@" >> "{argv}"; cat >> "{stdin}"; printf '200 application/json'; }}
    logger() {{ shift; echo "$*" >> "{logs}"; }}
    sleep() {{ :; }}
    {call}
    echo "rc=$? ok=${{KUMA_PUSH_OK:-unset}} key=${{HC_PING_KEY:-unset}}"
    """
    result = run(["bash", "-c", script])
    return (
        result,
        argv.read_text().splitlines(),
        stdin.read_text(),
        logs.read_text().splitlines(),
    )


def test_the_render_bakes_the_fleet_constants(rendered_lib):
    assert 'KUMA_LIB_HOST="uptime-kuma.local.' in rendered_lib
    assert f'KUMA_LIB_RESOLVE_IP="{VIP}"' in rendered_lib
    assert "{{" not in rendered_lib and "{%" not in rendered_lib


def test_the_token_form_builds_the_url_and_the_vip_pin(tmp_path, rendered_lib):
    result, argv, stdin, logs = _call(
        tmp_path, rendered_lib, "kuma_push up hello tok123 my-tag"
    )
    assert "rc=0 ok=1" in result.stdout
    host = rendered_lib.split('KUMA_LIB_HOST="', 1)[1].split('"', 1)[0]
    assert stdin == f'url = "https://{host}/api/push/tok123"\n'
    assert f"{host}:443:{VIP}" in argv
    # The token reaches curl on stdin only, never as an argument.
    assert not any("tok123" in arg for arg in argv)
    assert logs == []


def test_the_six_argument_form_still_pushes_where_it_says(tmp_path, rendered_lib):
    result, argv, stdin, _ = _call(
        tmp_path,
        rendered_lib,
        "kuma_push up hello https://kuma.old/api/push/tok kuma.old 10.9.9.9 my-tag",
    )
    assert "rc=0 ok=1" in result.stdout
    assert stdin == 'url = "https://kuma.old/api/push/tok"\n'
    assert "kuma.old:443:10.9.9.9" in argv


@pytest.mark.parametrize("args", ["up hello tok", "up hello url host ip tag extra"])
def test_any_other_argument_count_drops_the_push_and_says_so(
    tmp_path, rendered_lib, args
):
    result, argv, _, logs = _call(tmp_path, rendered_lib, f"kuma_push {args}")
    assert "rc=0 ok=0" in result.stdout
    assert argv == []
    assert any("push dropped" in line for line in logs)


def test_hc_ping_up_pings_the_bare_slug_with_the_body(tmp_path, rendered_lib):
    result, argv, stdin, logs = _call(
        tmp_path,
        rendered_lib,
        "hc_ping my-slug up my-tag 'all good'",
        'HC_PING_KEY="k3y"\n',
    )
    assert "rc=0" in result.stdout
    assert stdin == 'url = "https://hc-ping.com/k3y/my-slug"\n'
    assert argv[argv.index("--data-raw") + 1] == "all good"
    assert not any("k3y" in arg for arg in argv)
    assert logs == []


def test_hc_ping_down_pings_fail(tmp_path, rendered_lib):
    _, _, stdin, _ = _call(
        tmp_path, rendered_lib, "hc_ping my-slug down my-tag msg", 'HC_PING_KEY="k3y"\n'
    )
    assert stdin == 'url = "https://hc-ping.com/k3y/my-slug/fail"\n'


def test_hc_ping_with_no_body_sends_none(tmp_path, rendered_lib):
    _, argv, stdin, _ = _call(
        tmp_path, rendered_lib, "hc_ping my-slug up my-tag", 'HC_PING_KEY="k3y"\n'
    )
    assert stdin  # it did ping
    assert "--data-raw" not in argv


def test_hc_ping_with_an_empty_body_still_sends_one(tmp_path, rendered_lib):
    # Omitted and empty are different calls: the omitted form is the deliberate no-body ping.
    _, argv, _, _ = _call(
        tmp_path, rendered_lib, "hc_ping my-slug up my-tag ''", 'HC_PING_KEY="k3y"\n'
    )
    assert argv[argv.index("--data-raw") + 1] == ""


@pytest.mark.parametrize("env_text", [None, 'HC_PING_KEY=""\n'])
def test_hc_ping_without_a_key_does_nothing(tmp_path, rendered_lib, env_text):
    result, argv, _, logs = _call(
        tmp_path, rendered_lib, "hc_ping my-slug down my-tag msg", env_text
    )
    assert "rc=0" in result.stdout
    assert argv == []
    assert logs == []


def test_hc_ping_keeps_the_key_out_of_the_callers_variables(tmp_path, rendered_lib):
    result, *_ = _call(
        tmp_path, rendered_lib, "hc_ping my-slug up my-tag", 'HC_PING_KEY="k3y"\n'
    )
    assert "key=unset" in result.stdout


def test_a_failed_ping_is_logged_and_returns_zero(tmp_path, rendered_lib):
    result, _, _, logs = _call(
        tmp_path,
        rendered_lib,
        "curl() { cat >/dev/null; return 22; }; hc_ping s down t",
        'HC_PING_KEY="k"\n',
    )
    assert "rc=0" in result.stdout
    # The logger stub drops `-t` and keeps the tag.
    assert logs == ["t healthchecks ping failed (slug=s status=down)"]
