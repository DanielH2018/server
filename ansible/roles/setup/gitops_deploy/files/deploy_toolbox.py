#!/usr/bin/env python3
"""Every process boundary `gitops_deploy.py` crosses, as one injectable object.

A test replaces one field of `DeployTools` and never a module attribute. The defaults are the
real implementations: `deploy_io`'s for git, the health gate and the staging scripts,
`deploy_alerts.post` for the webhook, `datetime.now` for the clock. **This module names
`gitops_deploy` nowhere**, at import time or later, which is what makes it a leaf the entry
module can import.

WHY `deploy_toolbox` AND NOT `deploy_tools`. `scripts/deploy_tools/` is a namespace package on
pytest's `pythonpath`, and a regular module always beats a namespace portion whatever the path
order — so a `deploy_tools.py` in this directory shadows it and `deploy_tags.py`'s
`from deploy_tools.exit_codes import ...` raises ModuleNotFoundError for the whole suite. The
class keeps the name the other seams use (`RotationTools`, `FindingsTools`).

WHY `fetch_ci_verdict` LIVES HERE AND TAKES ITS CONFIG AS KEYWORDS. It is the one boundary
whose exact request — the URL, the headers, and the fail-closed `pending` on any error — is
the property under test, so a test that replaced the whole field would stop checking it.
`default_tools` binds `require_ci`, `repo` and `contexts` from the one `Config` the entry
module already parsed, rather than re-reading config.env here: `load_config` LOGS when it
disarms the CI gate on an empty context list, and a second parse would print that line twice
per tick and collect its errors where `CONFIG.validate()` never sees them.

Stdlib only, like the rest of the deployer.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

import deploy_alerts
import deploy_io
from deploy_config import Config, log
from deploy_git import ci_verdict, github_auth_headers, github_token


def fetch_ci_verdict(
    sha: str, *, require_ci: bool, repo: str, contexts: frozenset[str]
) -> str:
    """`pass` / `pending` / `fail` for `sha`, from GitHub's check-runs API.

    Args:
        sha: the commit to read a verdict for.
        require_ci: False disarms the gate entirely and returns `pass`.
        repo: the `owner/name` slug the check-runs are read from.
        contexts: the check-run NAMES that must be green.

    Authenticated through `gh auth token` when the CLI is logged in (deploy_git.github_token
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
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gitops-deploy",
            **github_auth_headers(github_token(os.environ, subprocess.run)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log(f"CI status unavailable for {sha[:8]} ({e}) — deferring this tick")
        return "pending"
    return ci_verdict(payload.get("check_runs", []), contexts)


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
    `deploy_io.deploy`, `deploy_k8s` and `deploy_broad` build their own argv and call
    `deploy_io.run` qualified, so they are not fields here — the argv they build is what the
    suite asserts on, and a field would replace the builder rather than the process.
    """

    run: Callable[..., str] = deploy_io.run
    git_fetch: Callable[[str, str], subprocess.CompletedProcess] = deploy_io.git_fetch
    git_status: Callable[[str], subprocess.CompletedProcess] = deploy_io.git_status
    is_ancestor: Callable[[str, str, str], bool] = deploy_io.is_ancestor
    fetch_ci_verdict: Callable[[str], str] = _ci_unconfigured
    discord_post: Callable[[str, str], bool] = deploy_alerts.post
    service_healthy: Callable[..., bool] = deploy_io.service_healthy
    run_staging_scripts: Callable[..., tuple[int, int]] = deploy_io.run_staging_scripts
    emit_deploy_annotation: Callable[[set[str], str], None] = (
        deploy_io.emit_deploy_annotation
    )
    # `datetime.now`, NOT a zero-argument clock: `deploy_io.record_staging_tick` calls it with
    # the ledger's tzinfo, so a `lambda: datetime.now()` adapter would change what that
    # function receives.
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
