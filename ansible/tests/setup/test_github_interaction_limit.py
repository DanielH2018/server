#!/usr/bin/env python3
"""Run github-interaction-limit.sh for real against a stubbed GitHub and a recording Kuma push.

Same harness shape as test_github_ruleset_drift.py, for the same reason: what breaks in a
re-arm cron is its BRANCHING — whether a PUT that never happened can read as "armed". Every case
drives the rendered script; curl and sudo are shadowed on PATH, and the curl stub records the
method and body it was called with so the tests can see WHAT was sent, not only what came back.
"""

import json
import os
import subprocess

import jinja2
import pytest
from _helpers import ANSIBLE

TEMPLATE = (
    ANSIBLE / "roles/setup/gitops_deploy/templates/github-interaction-limit.sh.j2"
)
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
CRON_PATH_LINE = "export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LIB_STUB = """\
kuma_push() {
  printf '%s\\n%s\\n' "$1" "$2" > "$KUMA_PUSH_OUT"
}
"""


def _limit_body(limit, expires_at="2027-03-06T06:50:00Z"):
    """The object the endpoint returns after a PUT, in its documented shape."""
    return json.dumps(
        {"limit": limit, "origin": "repository", "expires_at": expires_at}
    )


def _run(
    tmp_path, curl_body=None, curl_rc=0, *, limit="collaborators_only", logged_in=True
):
    """Render, stub, run. Returns (exit_code, status, message, curl_argv)."""
    body = (
        jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
        .from_string(TEMPLATE.read_text())
        .render(
            domain="example.test",
            k3s_metallb_ingress_vip="10.0.0.240",
            sys_user="ubuntu",
            interaction_limit_push_token="stubtoken",
            gitops_deploy_github_repo="Example/repo",
            gitops_deploy_interaction_limit=limit,
            gitops_deploy_interaction_limit_expiry="six_months",
        )
    )

    assert REAL_LIB in body, (
        "the script no longer sources the shared Kuma push helper — this harness repoints that "
        "exact path, so a rename silently stops these tests exercising the push at all"
    )
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(LIB_STUB)
    body = body.replace(REAL_LIB, str(lib))

    binstub = tmp_path / "bin"
    binstub.mkdir()
    assert CRON_PATH_LINE in body, (
        "the cron PATH reset changed shape — this harness prepends its stub dir to that exact "
        "line, and without it the curl stub is never reached"
    )
    body = body.replace(
        CRON_PATH_LINE, f"export PATH={binstub}:/usr/local/bin:/usr/bin:/bin"
    )

    script = tmp_path / "github-interaction-limit.sh"
    script.write_text(body)
    script.chmod(0o755)

    # curl stub: records its argv (so a test can assert the method and body), prints curl_body,
    # exits curl_rc. `-sf` on the real one turns an HTTP error into a non-zero rc, so a 403
    # from a token without the scope and a transport failure both arrive here the same way.
    argv_out = tmp_path / "curl.argv"
    curl = binstub / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {argv_out}\n"
        f"printf '%s' {json.dumps(curl_body or '')}\n"
        f"exit {curl_rc}\n"
    )
    curl.chmod(0o755)

    # sudo stub: the token lookup is `sudo -n -u <user> -H gh auth token`. On the box these
    # tests run on the real sudo is passwordless and the real gh is logged in, so without the
    # stub a unit test would reach a live credential — and a live PUT against the real repo.
    sudo = binstub / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\nprintf 'stub-token\\n'\nexit 0\n"
        if logged_in
        else "#!/usr/bin/env bash\nexit 1\n"
    )
    sudo.chmod(0o755)

    out = tmp_path / "push.out"
    env = {
        **os.environ,
        "PATH": f"{binstub}:{os.environ['PATH']}",
        "KUMA_PUSH_OUT": str(out),
    }
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    argv = argv_out.read_text().splitlines() if argv_out.exists() else []
    if not out.exists():
        return proc.returncode, None, None, argv
    status, message = out.read_text().split("\n", 1)
    return proc.returncode, status, message.strip(), argv


def test_a_successful_put_reports_armed(tmp_path):
    """The accepting half: the API stores the declared limit -> up, naming the new expiry."""
    rc, status, msg, _ = _run(tmp_path, curl_body=_limit_body("collaborators_only"))
    assert rc == 0
    assert status == "up"
    assert "re-armed" in msg
    assert "2027-03-06" in msg


def test_the_put_carries_the_declared_limit_and_expiry(tmp_path):
    """What was SENT, not only what came back: a PUT of the declared body to the repo's URL."""
    _, _, _, argv = _run(tmp_path, curl_body=_limit_body("collaborators_only"))
    assert "PUT" in argv
    body = argv[argv.index("-d") + 1]
    assert json.loads(body) == {"limit": "collaborators_only", "expiry": "six_months"}
    assert "https://api.github.com/repos/Example/repo/interaction-limits" in argv
    # The token reaches curl through -K, never as a header argument.
    assert not any("Bearer" in a for a in argv)


def test_no_gh_login_is_unverified_not_armed(tmp_path):
    """No token means no PUT can happen: DOWN, and the message says nothing was re-armed."""
    rc, status, msg, argv = _run(tmp_path, logged_in=False)
    assert rc == 1
    assert status == "down"
    assert "NOT re-armed" in msg
    assert "UNVERIFIED" in msg
    assert argv == [], "curl must not be reached without a token"


def test_a_failed_put_is_unverified_not_armed(tmp_path):
    """A 403 from a token without the scope, or an unreachable API: never 'armed'."""
    rc, status, msg, _ = _run(tmp_path, curl_rc=22, curl_body="")
    assert rc == 1
    assert status == "down"
    assert "NOT re-armed" in msg
    assert "UNVERIFIED" in msg


def test_a_200_without_a_limit_is_a_bad_response(tmp_path):
    """An error object or a truncated body must not read as armed."""
    rc, status, msg, _ = _run(tmp_path, curl_body='{"message":"Not Found"}')
    assert rc == 1
    assert status == "down"
    assert "bad response" in msg


def test_a_stored_limit_that_differs_from_the_declared_one_is_flagged(tmp_path):
    """The one branch that would otherwise read 'armed' while the setting is wrong."""
    rc, status, msg, _ = _run(tmp_path, curl_body=_limit_body("existing_users"))
    assert rc == 1
    assert status == "down"
    assert "existing_users" in msg
    assert "NOT applied" in msg


def test_declaring_none_clears_the_limit_with_a_delete(tmp_path):
    """The way out: `none` is a DELETE, not a PUT of a value the API would reject."""
    rc, status, msg, argv = _run(tmp_path, limit="none")
    assert rc == 0
    assert status == "up"
    assert "cleared" in msg
    assert "DELETE" in argv
    assert "PUT" not in argv


def test_a_failed_delete_is_unverified(tmp_path):
    rc, status, msg, _ = _run(tmp_path, limit="none", curl_rc=22)
    assert rc == 1
    assert status == "down"
    assert "UNVERIFIED" in msg


@pytest.mark.parametrize(
    "key", ["gitops_deploy_interaction_limit", "gitops_deploy_interaction_limit_expiry"]
)
def test_the_declared_values_are_ones_the_api_accepts(key):
    """The defaults are what the cron sends daily; a typo there is a daily 422 and a red tile."""
    from lib import yaml_fast

    defaults = yaml_fast.safe_load(
        (ANSIBLE / "roles/setup/gitops_deploy/defaults/main.yml").read_text()
    )
    accepted = {
        "gitops_deploy_interaction_limit": {
            "none",
            "existing_users",
            "contributors_only",
            "collaborators_only",
        },
        "gitops_deploy_interaction_limit_expiry": {
            "one_day",
            "three_days",
            "one_week",
            "one_month",
            "six_months",
        },
    }
    assert defaults[key] in accepted[key]
