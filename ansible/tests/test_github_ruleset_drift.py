#!/usr/bin/env python3
"""Run github-ruleset-drift.sh for real against a stubbed GitHub and a recording Kuma push.

Exercised rather than pattern-matched, for the reason the repo keeps relearning: what breaks in
a drift check is its BRANCHING — which conditions report up, which report down, and whether an
unreachable source is distinguishable from a healthy one. A textual guard sees none of that, and
a check that reports "no drift" when it could not look is the exact failure this script exists to
prevent (see a-deadman-is-not-a-failure-report).

Every case below drives the real script. Only two absolute paths are repointed: the Kuma push
helper it sources, and `curl`, which is shadowed on PATH by a stub serving a canned body.
"""

import json
import os
import subprocess

import jinja2
import pytest
from _helpers import ANSIBLE

TEMPLATE = ANSIBLE / "roles/setup/gitops_deploy/templates/github-ruleset-drift.sh.j2"
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
CRON_PATH_LINE = "export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The declared set the script bakes in. Kept here as a literal rather than read from defaults:
# the point of these tests is the comparison, and a fixture that moves with the thing under test
# would pass no matter what the comparison did.
DECLARED = [
    "prek (lint + validate + tests + secrets)",
    "pull + boot changed images",
    "renovate config validator",
]

# kuma_push, recording instead of pushing. Signature from
# roles/setup/initial_setup/files/kuma-push-lib.sh: STATUS MSG PUSH_URL HOST RESOLVE_IP TAG.
LIB_STUB = """\
kuma_push() {
  printf '%s\\n%s\\n' "$1" "$2" > "$KUMA_PUSH_OUT"
}
"""


def _ruleset_body(contexts, enforcement="active"):
    """A ruleset payload in the shape the live endpoint returns."""
    return json.dumps(
        {
            "id": 20912512,
            "enforcement": enforcement,
            "rules": [
                {"type": "deletion"},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": c, "integration_id": 15368} for c in contexts
                        ]
                    },
                },
            ],
        }
    )


def _run(tmp_path, curl_body=None, curl_rc=0):
    """Render the script, stub curl + the push lib, run it. Returns (exit_code, status, message)."""
    body = (
        # trim_blocks matches Ansible's own template defaults. Without it the `{% for %}` around
        # the declared contexts leaves a blank line per iteration, which is NOT how the deployed
        # script renders — the harness would be testing a file the host never sees.
        jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
        .from_string(TEMPLATE.read_text())
        .render(
            domain="example.test",
            k3s_metallb_ingress_vip="10.0.0.240",
            sys_user="ubuntu",
            ruleset_drift_push_token="stubtoken",
            gitops_deploy_github_repo="Example/repo",
            gitops_deploy_ruleset_id=20912512,
            gitops_deploy_expected_ruleset_contexts=DECLARED,
        )
    )

    assert REAL_LIB in body, (
        "the script no longer sources the shared Kuma push helper — this harness repoints that "
        "exact path, so a rename silently stops these tests exercising the push at all"
    )
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(LIB_STUB)
    body = body.replace(REAL_LIB, str(lib))

    # The script resets PATH for cron, which would drop the stub dir this harness puts in the
    # environment — so prepend it inside that same line. Asserted rather than best-effort: if the
    # reset moves or changes shape, every case below would silently take the curl-failure branch
    # and still "pass" the DOWN assertions, which is precisely the inert-check shape these tests
    # exist to rule out.
    binstub = tmp_path / "bin"
    binstub.mkdir()
    assert CRON_PATH_LINE in body, (
        "the cron PATH reset changed shape — this harness prepends its stub dir to that exact "
        "line, and without it the curl stub is never reached"
    )
    body = body.replace(
        CRON_PATH_LINE, f"export PATH={binstub}:/usr/local/bin:/usr/bin:/bin"
    )

    script = tmp_path / "github-ruleset-drift.sh"
    script.write_text(body)
    script.chmod(0o755)

    # curl stub: exits curl_rc, prints curl_body. `-sf` means the real one exits non-zero on an
    # HTTP error, so a transport failure and a 404 both arrive here as a non-zero rc.
    curl = binstub / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' {json.dumps(curl_body or '')}\n"
        f"exit {curl_rc}\n"
    )
    curl.chmod(0o755)

    out = tmp_path / "push.out"
    env = {
        **os.environ,
        "PATH": f"{binstub}:{os.environ['PATH']}",
        "KUMA_PUSH_OUT": str(out),
    }
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    if not out.exists():
        return proc.returncode, None, None
    status, message = out.read_text().split("\n", 1)
    return proc.returncode, status, message.strip()


def test_matching_ruleset_is_clean(tmp_path):
    """The accepting half: live set equals the declared set, enforcement active -> up."""
    rc, status, msg = _run(tmp_path, curl_body=_ruleset_body(DECLARED))
    assert rc == 0
    assert status == "up"
    assert "matches the declared set" in msg


def test_a_removed_context_is_flagged(tmp_path):
    """The dangerous direction: a required check dropped in the UI stops gating merges."""
    rc, status, msg = _run(tmp_path, curl_body=_ruleset_body(DECLARED[:-1]))
    assert rc == 1
    assert status == "down"
    assert "DRIFTED" in msg
    assert "renovate config validator" in msg
    # Named in the no-longer-required half, not the newly-required one.
    assert msg.index("no-longer-required") < msg.index("renovate config validator")


def test_an_added_context_is_flagged(tmp_path):
    """The other direction: something now required that this repo does not declare."""
    rc, status, msg = _run(tmp_path, curl_body=_ruleset_body([*DECLARED, "new gate"]))
    assert rc == 1
    assert status == "down"
    assert "newly-required" in msg
    assert "new gate" in msg


def test_an_unreachable_api_reports_unverified_not_clean(tmp_path):
    """The failure this check exists to avoid being: silence read as a pass.

    A fetch that never happened must never produce "no drift". It reports DOWN and says the gate
    is UNVERIFIED, which is a different claim from "the gate is wrong".
    """
    rc, status, msg = _run(tmp_path, curl_rc=7, curl_body="")
    assert rc == 1
    assert status == "down"
    assert "UNVERIFIED" in msg
    assert "DRIFTED" not in msg


def test_a_200_that_is_not_a_ruleset_is_a_bad_fetch(tmp_path):
    """A truncated body or an error object must not read as "every check was removed"."""
    rc, status, msg = _run(tmp_path, curl_body='{"message":"Not Found"}')
    assert rc == 1
    assert status == "down"
    assert "bad fetch" in msg
    assert "UNVERIFIED" in msg


def test_zero_required_contexts_is_flagged(tmp_path):
    """A well-formed ruleset that requires nothing: every merge gate is open."""
    rc, status, msg = _run(tmp_path, curl_body=_ruleset_body([]))
    assert rc == 1
    assert status == "down"
    assert "NO status checks" in msg


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled"])
def test_inactive_enforcement_is_flagged(tmp_path, enforcement):
    """Contexts can all be present while the ruleset enforces none of them."""
    rc, status, msg = _run(
        tmp_path, curl_body=_ruleset_body(DECLARED, enforcement=enforcement)
    )
    assert rc == 1
    assert status == "down"
    assert enforcement in msg


def test_the_declared_set_matches_the_role_defaults(tmp_path):
    """DECLARED above is a fixture; the deployed list lives in defaults. Keep them honest.

    Not a tautology: this is the one assertion that ties the cases above to the real config, so a
    context added to defaults without a thought about the comparison shows up here.
    """
    import yaml

    defaults = yaml.safe_load(
        (ANSIBLE / "roles/setup/gitops_deploy/defaults/main.yml").read_text()
    )
    assert defaults["gitops_deploy_expected_ruleset_contexts"] == DECLARED, (
        "gitops_deploy_expected_ruleset_contexts drifted from this test's fixture — if the "
        "ruleset genuinely changed, update both; the monitor compares this list against GitHub"
    )
