"""The harness every github-ruleset-drift.sh test drives: a rendered script, a stubbed GitHub.

Only two absolute paths in the script are repointed: the Kuma push helper it sources, and
`curl`, which a stub on PATH shadows to serve one canned body per ruleset id. The fixtures are
the live bodies `gh api repos/DanielH2018/server/rulesets/<id>` returned, less the links, node id
and timestamps, so a case edits a real shape rather than a hand-built one.
"""

import json
import os
import subprocess

from lib.proc_testing import fake_bin, path_with, write_exec
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


def ruleset_body(contexts, enforcement="active", bypass_actors=()):
    """A ruleset payload in the shape the live endpoint returns to an admin token.

    `bypass_actors=None` is the anonymous shape, where GitHub returns null for the list.
    """
    return json.dumps(
        {
            "id": 20912512,
            "enforcement": enforcement,
            "bypass_actors": None if bypass_actors is None else list(bypass_actors),
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


def branch_ruleset_body(exclude=(RENOVATE_EXCLUDE,), enforcement="active"):
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


REVIEW_RULESET_ID = 24514824
REVIEW_BYPASS = ["Integration:2740:pull_request", "RepositoryRole:5:pull_request"]


def review_ruleset(**overrides):
    """The review ruleset as the live endpoint returned it on 2026-10-05, after the operator set
    require_last_push_approval. Returned as a dict for a case to edit."""
    body = {
        "id": REVIEW_RULESET_ID,
        "name": "master review gate",
        "target": "branch",
        "source_type": "Repository",
        "source": "DanielH2018/server",
        "enforcement": "active",
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/master"]}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "required_reviewers": [],
                    "require_code_owner_review": False,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": False,
                    "require_extra_approval_for_unattributed_changes": True,
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            }
        ],
        "bypass_actors": [
            {
                "actor_id": 5,
                "actor_type": "RepositoryRole",
                "bypass_mode": "pull_request",
            },
            {
                "actor_id": 2740,
                "actor_type": "Integration",
                "bypass_mode": "pull_request",
            },
        ],
        "current_user_can_bypass": "pull_requests_only",
    }
    body.update(overrides)
    return body


FENCE_RULESET_ID = 24517167
FENCE_EXCLUDE = ["refs/heads/worktree-claude/**"]
FENCE_BYPASS = ["Integration:2740:always", "RepositoryRole:5:always"]


def fence_ruleset(**overrides):
    """The agent branch fence as the live endpoint returned it on 2026-10-05, as a dict."""
    body = {
        "id": FENCE_RULESET_ID,
        "name": "agent branch fence",
        "target": "branch",
        "source_type": "Repository",
        "source": "DanielH2018/server",
        "enforcement": "active",
        "conditions": {"ref_name": {"exclude": FENCE_EXCLUDE, "include": ["~ALL"]}},
        "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
        "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
            {"actor_id": 2740, "actor_type": "Integration", "bypass_mode": "always"},
        ],
        "current_user_can_bypass": "always",
    }
    body.update(overrides)
    return body


def run(
    tmp_path,
    curl_body=None,
    curl_rc=0,
    branch_body=None,
    review_body=None,
    fence_body=None,
):
    """Render the script, stub curl + the push lib, run it. Returns (exit_code, status, message).

    `curl_body` answers the merge-gate ruleset fetch, and `branch_body`, `review_body` and
    `fence_body` the other three. Those three default to a clean body, so each case reads the
    verdict of the arm it is about. Every value the template reads comes from the inventory and
    the role's own defaults, which the shared accessor resolves. The `..._matches_the_role_defaults`
    tests in test_github_ruleset_drift.py keep this module's constants honest against them
    (#3178).
    """
    if branch_body is None:
        branch_body = branch_ruleset_body()
    if review_body is None:
        review_body = json.dumps(review_ruleset())
    if fence_body is None:
        fence_body = json.dumps(fence_ruleset())
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
            f"  */rulesets/{REVIEW_RULESET_ID}) printf '%s' {json.dumps(review_body)} ;;\n"
            f"  */rulesets/{FENCE_RULESET_ID}) printf '%s' {json.dumps(fence_body)} ;;\n"
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
