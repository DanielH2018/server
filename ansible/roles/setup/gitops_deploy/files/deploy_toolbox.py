#!/usr/bin/env python3
"""Every process boundary `gitops_deploy.py` crosses, as one injectable object.

A test replaces one field of `DeployTools` and never a module attribute. The defaults are the
real implementations: `deploy_io`'s for git and the health gate,
`post` below for the webhook, `datetime.now` for the clock. **This module names
`gitops_deploy` nowhere**, at import time or later, which is what makes it a leaf the entry
module can import.

WHY `deploy_toolbox` AND NOT `deploy_tools`. `scripts/deploy_tools/` is a namespace package on
pytest's `pythonpath`, and a regular module always beats a namespace portion whatever the path
order — so a `deploy_tools.py` in this directory shadows it and `land_lib/tools.py`'s
`from deploy_tools.deploy_lib.detach_notify import ...` raises ModuleNotFoundError for the whole suite. The
class keeps the name the other seams use (`RotationTools`, `FindingsTools`).

WHY `fetch_ci_verdict` LIVES HERE AND TAKES ITS CONFIG AS KEYWORDS. It is the one boundary
whose exact request — the URL, the headers, and the fail-closed `pending` on any error — is
the thing a test replacing the whole field would stop exercising, so the two halves are tested
apart: `test_deploy_git_ci.py` covers the pure `ci_verdict` reduction and
`test_deploy_toolbox_ci_request.py` drives this request path through a stubbed `urlopen`.
`default_tools` binds `require_ci`, `repo` and `contexts` from the one `Config` the entry
module already parsed, rather than re-reading config.env here: `load_config` LOGS when it
disarms the CI gate on an empty context list, and a second parse would print that line twice
per tick and collect its errors where `CONFIG.validate()` never sees them.

Stdlib only, like the rest of the deployer.
"""

import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

import deploy_io
import deploy_narrow
import deploy_release
import deploy_setup_roles
from deploy_config import Config, log
from deploy_git import ci_verdict
from host_lib import discord_post, flush_discord_spool, github_get, github_token


def post(webhook: str, content: str, log_fn=log) -> bool:
    """Post to the alert webhook via the shared host_lib.discord_post.

    See there for the Cloudflare-1010 User-Agent + 2xx-only-success contract the per-SHA
    dedupe markers gate on. A missing webhook or any error returns False, so the alert is
    retried on the next tick.

    It lives HERE rather than in `deploy_alerts` because it is a process boundary, and because
    `deploy_alerts` now holds the alert ORCHESTRATION (`deliver`, `alert_once`, the per-SHA
    markers) and so takes a `DeployTools`. An import in that direction plus the old
    `deploy_toolbox -> deploy_alerts` edge would be a cycle; moving the webhook down here is
    what removes it.
    """
    return discord_post(webhook, content, "gitops-deploy", log=log_fn)


def flush_spool(spool_dir: str, webhook: str, log_fn=log) -> bool:
    """Send what another program queued in `spool_dir`, via the shared host_lib flush.

    `deploy_alerts.flush_detach_spool` is the caller, and the queue is `deploy --detach`'s,
    whose notifier has no next run of its own. Each queued message already carries the
    notifier's `deploy --detach:` marker; the user agent is the deployer's, which sends it.
    """
    return flush_discord_spool(spool_dir, webhook, "gitops-deploy", log=log_fn)


def fetch_ci_verdict(
    sha: str, *, require_ci: bool, repo: str, contexts: frozenset[str]
) -> str:
    """`pass` / `pending` / `fail` for `sha`, from GitHub's check-runs API.

    Args:
        sha: the commit to read a verdict for.
        require_ci: False disarms the gate entirely and returns `pass`.
        repo: the `owner/name` slug the check-runs are read from.
        contexts: the check-run NAMES that must be green.

    Authenticated through `gh auth token` when the CLI is logged in (host_lib.github_token
    says why: the anonymous 60/hour limit is per source IP and shared with every landing's
    `await_ci.py` poll, and two landings exhaust it), anonymous otherwise.

    An unreachable or malformed API reads as `pending`, never `pass`: the gate has to fail closed
    or it is not a gate. That defers the tick and retries in 30 minutes, and because the tick still
    completes normally (writing `last_run`), a GitHub outage does NOT trip GitOps-Alive the way a
    RetryableFetchError would. Sustained unavailability instead leaves the host behind origin,
    which `behind_marker` records and the 6h behind-origin watchdog pages on.
    """
    if not require_ci:
        return "pass"
    try:
        payload = github_get(
            f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
            github_token(os.environ, subprocess.run),
            user_agent="gitops-deploy",
        )
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log(f"CI status unavailable for {sha[:8]} ({e}) — deferring this tick")
        return "pending"
    return ci_verdict(payload.get("check_runs", []), contexts)


def github_authenticated() -> bool:
    """Whether this host's GitHub reads carry a token.

    The ancestor walk asks it before spending up to `CI_ANCESTOR_WALK_MAX` requests on one
    tick. Anonymous, the whole host shares 60 requests an hour with every landing's
    `await_ci.py` poll (45 per 900s wait) and the two GitHub crons — a burst of ten would
    exhaust it and every reader's next call reads `HTTP Error 403`, which this gate maps to
    `pending` and a landing reads as "CI not finished". Authenticated it is 5000/hour, where
    ten reads per ten-minute tick is nothing.
    """
    return github_token(os.environ, subprocess.run) is not None


def _ci_unconfigured(sha: str) -> str:
    """`pending` for a `DeployTools` built without a `Config`.

    Fail closed, never `pass`: the unbound default is what a caller gets when it forgets
    `default_tools`, and the CI gate's whole contract is that an unknown verdict defers.
    """
    log(
        f"CI verdict for {sha[:8]} requested from an unconfigured DeployTools — deferring"
    )
    return "pending"


@dataclass(frozen=True)
class DeployTools:
    """Every boundary one tick crosses, so a test replaces a field and not a module.

    The defaults are production. `run` is the one that also reaches subprocess indirectly:
    `deploy_io.deploy_k8s` and `deploy_broad` build their own argv and take `run=tools.run`,
    so they are not fields here — the argv they build is what the suite asserts on, and a
    field would replace the builder rather than the process.
    """

    run: Callable[..., str] = deploy_io.run
    git_fetch: Callable[[str, str], subprocess.CompletedProcess] = deploy_io.git_fetch
    git_status: Callable[[str], subprocess.CompletedProcess] = deploy_io.git_status
    is_ancestor: Callable[[str, str, str], bool] = deploy_io.is_ancestor
    # The applied commit from a service's k8s release record, for the `k8s_unapplied`
    # discharge. A field because the record is written by a DIFFERENT process — an operator's
    # `deploy.sh` — so the suite has to script it rather than stage a file.
    release_commit: Callable[[str], str | None] = deploy_release.release_commit
    # The commit a matching render proves a service's applied bytes for (#3057), read from the
    # render record an hourly producer writes. A field for the reason `release_commit` is one.
    render_proof: Callable[[str], str | None] = deploy_release.render_proof
    # The render-digest verdicts at one commit, for the deploy-plane shadow log (#3045).
    digest_diff: Callable[[str], dict[str, list[str]]] = deploy_release.digest_diff
    # The tags whose deploy runs a shared role (#2643), which has no record of its own.
    # A subprocess that parses YAML, so a field for the reason `narrow_setup_role` is one.
    shared_role_callers: Callable[..., dict[str, set[str]]] = (
        deploy_narrow.shared_role_callers
    )
    # The roles whose own render digest may discharge their line (#3110). A YAML-parsing
    # subprocess too, and a field for the same reason.
    digest_provable: Callable[..., set[str]] = deploy_narrow.digest_provable
    fetch_ci_verdict: Callable[[str], str] = _ci_unconfigured
    # Production default, unlike `fetch_ci_verdict` above: this one needs no `Config`.
    github_authenticated: Callable[[], bool] = github_authenticated
    discord_post: Callable[[str, str], bool] = post
    flush_discord_spool: Callable[[str, str], bool] = flush_spool
    # The deploy-plane narrowing, which is a subprocess because the derivation parses YAML
    # and this unit runs under `uv run --no-project`. A field rather than a qualified call,
    # so the broad arm's tests script an exit code instead of a process.
    narrow_deploy_plane: Callable[..., tuple[int, str]] = (
        deploy_narrow.narrow_deploy_plane
    )
    # The setup-role narrowing, a subprocess for the same reason and a field for the same
    # reason: `deploy_defer.record`'s tests script an exit code rather than a process.
    narrow_setup_role: Callable[..., tuple[int, str]] = deploy_narrow.narrow_setup_role
    # Which playbook and tag apply each setup role (#3734): a subprocess that parses the
    # playbooks, and a field so a tick test scripts the routing rather than a process.
    setup_routing: Callable[[str, str, str], tuple[dict, dict]] = (
        deploy_setup_roles.derive_routing
    )
    emit_deploy_annotation: Callable[[set[str], str], None] = (
        deploy_io.emit_deploy_annotation
    )
    # `datetime.now`, NOT a zero-argument clock: `deploy_handlers.handle_dirty` calls it with
    # `CHICAGO`, so a `lambda: datetime.now()` adapter would change what that call receives.
    now: Callable[..., datetime] = datetime.now


def default_tools(config: Config) -> DeployTools:
    """The production `DeployTools`, with the CI gate bound to `config`."""
    return DeployTools(
        fetch_ci_verdict=partial(
            fetch_ci_verdict,
            require_ci=config.require_ci,
            repo=config.ci_repo,
            contexts=config.ci_contexts,
        )
    )
