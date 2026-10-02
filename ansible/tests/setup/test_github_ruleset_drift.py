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

import pytest
from lib.proc_testing import fake_bin, path_with, write_exec
from _helpers import ANSIBLE
from _shell_render import render_shell_script

TEMPLATE = ("setup", "gitops_deploy", "github-ruleset-drift.sh.j2")
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
# `reachout_verdict` from the real library, stubbed to the WAN-reachable answer — which is what
# every case here means: the source refused, so the tile must page. The skip path is covered by
# ansible/tests/setup/test_kuma_push_wan_skip.py against the real function.
reachout_verdict() { REACHOUT_STATUS=down; REACHOUT_NOTE=""; }
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


BRANCH_RULESET_ID = 17358590
RENOVATE_EXCLUDE = "refs/heads/renovate/**"


def _branch_ruleset_body(exclude=(RENOVATE_EXCLUDE,), enforcement="active"):
    """The branch-protection ruleset, in the shape the live endpoint returns."""
    return json.dumps(
        {
            "id": BRANCH_RULESET_ID,
            "name": "Default",
            "enforcement": enforcement,
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": list(exclude)}},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
        }
    )


def _run(tmp_path, curl_body=None, curl_rc=0, branch_body=None):
    """Render the script, stub curl + the push lib, run it. Returns (exit_code, status, message).

    `curl_body` answers the merge-gate ruleset fetch; `branch_body` answers the branch-protection
    one, and defaults to a body carrying the Renovate exclusion so the merge-gate cases above
    keep reading the verdict they are about. Every value the template reads — the declared
    contexts, both ruleset ids, the Renovate exclusion, `wan_probe_urls` — comes from the
    inventory and the role's own defaults, the shared accessor already resolves them, and
    `test_the_declared_set_matches_the_role_defaults` / `test_the_branch_ruleset_fixture_matches_the_role_defaults`
    below are what keeps DECLARED/BRANCH_RULESET_ID/RENOVATE_EXCLUDE honest against them (#3178).
    """
    if branch_body is None:
        branch_body = _branch_ruleset_body()
    body = render_shell_script(*TEMPLATE)

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
    binstub.mkdir(parents=True, exist_ok=True)
    assert CRON_PATH_LINE in body, (
        "the cron PATH reset changed shape — this harness prepends its stub dir to that exact "
        "line, and without it the curl stub is never reached"
    )
    body = body.replace(
        CRON_PATH_LINE, f"export PATH={binstub}:/usr/local/bin:/usr/bin:/bin"
    )

    script = write_exec(tmp_path / "github-ruleset-drift.sh", body)

    # curl stub: exits curl_rc, prints the body for whichever ruleset the URL names. `-sf` means
    # the real one exits non-zero on an HTTP error, so a transport failure and a 404 both arrive
    # here as a non-zero rc. The URL is the last argument the script passes.
    fake_bin(
        binstub,
        curl=(
            "#!/usr/bin/env bash\n"
            f'case "${{@: -1}}" in\n'
            f"  */rulesets/{BRANCH_RULESET_ID}) printf '%s' {json.dumps(branch_body)} ;;\n"
            f"  *) printf '%s' {json.dumps(curl_body or '')} ;;\n"
            "esac\n"
            f"exit {curl_rc}\n"
        ),
    )

    # sudo stub: the script's token lookup is `sudo -n -u <user> -H gh auth token`, and on the
    # box these tests run on the real sudo is passwordless and the real gh is logged in — so
    # without this stub a unit test reaches out to a live credential. The stub fails the way
    # a host with no gh login does, which is the anonymous path every case here exercises.
    fake_bin(binstub, sudo="#!/usr/bin/env bash\nexit 1\n")

    # logger stub: the script's journal line. Recorded rather than sent, so a case can assert the
    # verdict reached the journal without reading the host's own. The real logger is what a
    # `journalctl -t github-ruleset-drift` on the deployer reads, which is how a verify-by
    # confirms this producer ran.
    fake_bin(
        binstub,
        logger=(
            "#!/usr/bin/env bash\n"
            "shift 2  # -t <tag>\n"
            'printf \'%s\\n\' "$*" >> "$LOGGER_OUT"\n'
        ),
    )

    out = tmp_path / "push.out"
    env = {
        **os.environ,
        "PATH": path_with(binstub),
        "KUMA_PUSH_OUT": str(out),
        "LOGGER_OUT": str(tmp_path / "journal.out"),
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


def _journal(tmp_path):
    path = tmp_path / "journal.out"
    return path.read_text().splitlines() if path.exists() else []


def test_a_clean_run_reaches_the_journal(tmp_path):
    """The accepting half of the journal line: a clean run logs its verdict, not only a failed one.

    The push library logs a push only when it fails, so without this line a clean run leaves no
    trace on the host and a verify-by of "confirmed green from its journal" cannot be met.
    """
    _run(tmp_path, curl_body=_ruleset_body(DECLARED))
    lines = _journal(tmp_path)
    assert len(lines) == 1
    assert lines[0].startswith("status=up ")
    assert "matches the declared set" in lines[0]


def test_a_down_run_reaches_the_journal_with_its_reason(tmp_path):
    """The rejecting half: a DOWN logs the same reason the tile shows."""
    _run(tmp_path, curl_body=_ruleset_body(DECLARED[1:]))
    lines = _journal(tmp_path)
    assert len(lines) == 1
    assert lines[0].startswith("status=down ")
    assert "DRIFTED" in lines[0]


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


def test_a_missing_renovate_exclusion_is_flagged(tmp_path):
    """The rejecting half of the branch-ruleset arm: merge gate clean, exclusion absent -> down.

    With `renovate/**` under the deletion and non_fast_forward rules, Renovate can neither
    delete a merged branch nor rebase it, so the next PR reuses a SHA that already carries a
    green verdict and automerges empty."""
    rc, status, msg = _run(
        tmp_path,
        curl_body=_ruleset_body(DECLARED),
        branch_body=_branch_ruleset_body(exclude=()),
    )
    assert rc == 1
    assert status == "down"
    assert f"ruleset {BRANCH_RULESET_ID} does not exclude {RENOVATE_EXCLUDE}" in msg


def test_a_narrower_exclusion_is_not_the_exclusion(tmp_path):
    """`refs/heads/renovate/*` is one level; the app's branches nest. Literal match only."""
    rc, status, msg = _run(
        tmp_path,
        curl_body=_ruleset_body(DECLARED),
        branch_body=_branch_ruleset_body(exclude=("refs/heads/renovate/*",)),
    )
    assert rc == 1
    assert status == "down"
    assert "does not exclude" in msg


def test_an_unreachable_branch_ruleset_reports_unverified_not_clean(tmp_path):
    """Merge gate fetched and clean, branch ruleset fetch fails -> down UNVERIFIED, never up."""
    rc, status, msg = _run(
        tmp_path,
        curl_body=_ruleset_body(DECLARED),
        branch_body="",
    )
    # An empty body parses to no `.enforcement`, which is the bad-fetch branch.
    assert rc == 1
    assert status == "down"
    assert f"ruleset {BRANCH_RULESET_ID}" in msg
    assert "UNVERIFIED" in msg


def test_the_branch_ruleset_fixture_matches_the_role_defaults(tmp_path):
    """Same honesty check as below, for the second ruleset's id and pattern."""
    import yaml

    defaults = yaml.safe_load(
        (ANSIBLE / "roles/setup/gitops_deploy/defaults/main.yml").read_text()
    )
    assert defaults["gitops_deploy_branch_ruleset_id"] == BRANCH_RULESET_ID
    assert defaults["gitops_deploy_branch_ruleset_renovate_exclude"] == RENOVATE_EXCLUDE


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
