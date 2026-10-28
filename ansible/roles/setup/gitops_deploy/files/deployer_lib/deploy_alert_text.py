# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_alert_text.py
"""Every message body the deployer posts: one pure function per alert.

The seam `deploy_alerts.py`'s docstring already named. **What to say** is here — no I/O, no
state, no config — so `tests/test_gitops_deploy_failure_output.py` can assert an assembled
post stays inside host_lib.discord_post's 1900-character cut with its remediation line
intact, without driving a tick. **When to say it** stayed in `deploy_alerts.py`: `alert_once`,
`deliver`, `drain_pending` and `alert_deferred`.

It reads alone. Every function here takes the facts of one alert and returns its text, and
nothing here reads `DeployerState`, the retry queue or the webhook (#2600).

Reach these functions qualified (`deploy_alert_text.crash_alert(...)`), never by from-import —
see `deploy_io.py`'s docstring for why.
"""

from deployer_lib.deploy_failtext import TimedOutWithOutput, failing_task, head, tail
from gitops_markers import HOLD_CLEAR_CMD, PLAYBOOK_CMD

# Per-alert budget for an embedded error string. host_lib.discord_post cuts a post at
# `message[:1900]`, keeping the HEAD — so an unbounded error string does not truncate itself, it
# evicts the remediation prose that follows it. Sized for the longest of the three failure posts.
ALERT_EXCERPT_CHARS = 700

# ── error text, bounded for a Discord post ────────────────────────────────────────────────────


def alert_excerpt(exc: BaseException, limit: int = ALERT_EXCERPT_CHARS) -> str:
    """Render ``exc`` for a Discord post: the argv line, then the failing task or the tail.

    The first line of a run() RuntimeError is the argv and the exit code, which a plain tail
    would drop. The rest is process output, and run() put the failing task at the start of it
    when there was one, so a tail here would evict it again behind stderr's deprecation
    warnings — the same output a positional tail is the wrong slice of.
    """
    first, newline, body = str(exc).partition("\n")
    first = first[:limit]
    if not newline:
        return first
    budget = max(limit - len(first), 0)
    if isinstance(exc, TimedOutWithOutput):
        # A killed run has no `fatal:` line for failing_task to find, and its body is already
        # ordered running-task-first by `running_task_detail`. Head, not tail: a tail here
        # would post the end of stderr's deprecation warnings and drop the task header.
        return f"{first}\n{head(body, budget)}"
    found = failing_task(body)
    if found is None:
        return f"{first}\n{tail(body, budget)}"
    return f"{first}\n{head(found[0], budget)}"


# ── the message bodies ────────────────────────────────────────────────────────────────────────
# One function per alert. Pure, so `tests/test_gitops_deploy_failure_output.py` can assert the
# assembled post stays inside host_lib.discord_post's 1900-character cut with its remediation
# line intact — the reason `broad_failure_alert` was a function before the rest of them were.


def crash_alert(exc: BaseException) -> str:
    """The generic page for an unexpected exception, sent before the traceback propagates."""
    return f"🚨 gitops-deploy crashed: {exc}"


def bad_config_alert(hostname: str, config_path: str, exc: BaseException) -> str:
    """The page for a config file the deployer cannot be run with.

    A malformed numeric value raised during IMPORT until `Config` moved the parse behind
    `load_config`, so the failure reached an operator as a traceback with no key name in it and
    no Discord post at all — the unit's OnFailure curl was the whole signal.
    """
    return (
        f"🚨 gitops-deploy: **bad config** on {hostname}. `{config_path}` — {exc}.\n"
        f"No tick ran. Re-render it with `{PLAYBOOK_CMD} "
        f"ansible/initial_setup.yml --tags gitops_deploy`."
    )


def hold_clear_note(sha: str) -> str:
    """How a hand clears the hold on `sha` once every held plane is applied.

    The planes are lines of the `owed` ledger (#3392), so removing `hold_sha` by hand would
    leave them behind to re-hold the next failure. The deploy UI's Clear and
    `gitops_state.py clear-hold` both remove `hold_sha` and every held plane together, under
    the git-tree lock (#3930). The shell form takes the FULL held SHA and refuses any other,
    so `sha` is never sliced here (#4015).
    """
    return (
        "Once every held plane is applied, clear the hold with **Clear** in the deploy UI "
        f"(deploy.local) or `{HOLD_CLEAR_CMD} {sha}`; a hand `rm` of the hold leaves its "
        "planes in `owed.jsonl`."
    )


def broad_failure_alert(
    hostname: str,
    playbook: str,
    tags: list[str],
    origin: str,
    exc: BaseException,
) -> str:
    """The post for a failed broad apply.

    A function rather than an inline f-string so the 1900-char budget it is sized against is
    testable — see tests/test_gitops_deploy_failure_output.py.
    """
    tag_note = f" --tags `{','.join(tags)}`" if tags else ""
    return (
        f"🚨 gitops-deploy: **broad apply failed** on {hostname}.\n"
        f"`{playbook}`{tag_note} errored on `{origin[:8]}`:\n"
        f"```\n{alert_excerpt(exc)}\n```\n"
        f"The tree is fast-forwarded and the plane is **unapplied**. This arm is "
        f"forward-only — **nothing was rolled back**, so live state is whatever the "
        f"failed run left.\n"
        f"**Action:** fix forward and re-run that playbook by hand. A later tick clears each "
        f"held plane by applying it or deploying the services it tags. "
        f"{hold_clear_note(origin)}"
    )


def broad_deferred_alert(
    origin: str, remediation: str, secret_readers_failed: bool = False
) -> str:
    """The post for a broad change this deployer will not apply itself.

    A park on a failed `secrets.yml` reader lookup (#5436) says "run X to clear it" nowhere,
    because X would be a hand merge past the key. `remediation` is the retry text there.
    """
    if secret_readers_failed:
        return (
            f"⚠️ gitops-deploy: `{origin[:8]}` parked, nothing merged — the lookup of which "
            f"setup role reads the changed `secrets.yml` key failed. {remediation}"
        )
    return (
        f"⚠️ gitops-deploy: broad change needing a hand in `{origin[:8]}` — "
        f"deferring to a manual deploy. On the host, run "
        f"{remediation} "
        f"to clear it."
    )


def manual_plane_alert(origin: str, remediation: str, marker: str) -> str:
    """The post for a setup role the tick fast-forwarded past and cannot apply itself.

    `broad_deferred_alert`'s text plus the two facts that differ from a park: the range IS
    merged, so nobody else's landing is waiting on this, and the role is recorded in a file
    that pages on its own age — so an unread page is no longer the only signal.
    """
    return broad_deferred_alert(origin, remediation) + (
        f" The range is merged; the role is recorded in `{marker}` and pages "
        "**GitOps Deploy — Status** once the oldest entry is six hours old."
    )


def secrets_deferred_alert(origin: str) -> str:
    """The post for a merged `secrets.yml` change, sent whatever the tick applied beside it.

    A tick redeploys the k8s readers of each changed key (#5409, #5577) and applies the setup
    roles reading one that this host runs (#5436). Neither covers a reader the derivation
    refused, a setup role gated onto another host, or a Pi service. So the post claims no
    consumer, and names the command that lists every consumer with the repair for its own
    plane.
    """
    return (
        f"⚠️ gitops-deploy: `secrets.yml` changed in `{origin[:8]}`. A changed secret reaches "
        f"a consumer only when that consumer renders again: a service or setup role this "
        f"tick applied has it, and **every other consumer still holds the old "
        f"value**. For each changed key, `uv run python scripts/secrets_mgmt/secret_rotation.py "
        f"consumers <secret>` names its consumers and the command for each plane — "
        f"`{PLAYBOOK_CMD} ansible/deploy.yml --tags <svc>` for a service, "
        f"`{PLAYBOOK_CMD} ansible/initial_setup.yml --tags <role>` for a setup role."
    )


def tasks_deferred_alert(origin: str, services: set[str]) -> str:
    """The post for a structural-dir change to services this tick did not redeploy."""
    return (
        f"⚠️ gitops-deploy: a structural dir (`tasks/`/`defaults/`/`vars/`/`handlers/`) changed "
        f"for `{', '.join(sorted(services))}` in `{origin[:8]}` with no redeploy of those "
        f"service(s) — fast-forwarded but **not applied** (those dirs aren't auto-deployed). "
        f"Redeploy by hand: `{PLAYBOOK_CMD} ansible/deploy.yml --tags <svc>`."
    )


def dirty_tree_alert(hostname: str) -> str:
    """The twice-daily reminder that an operator's edit is parking this host."""
    return (
        f"⚠️ gitops-deploy: working tree dirty on {hostname} — skipping. "
        "Resolve manually."
    )


def ci_failed_alert(hostname: str, local: str, origin: str) -> str:
    """The post for a master tip whose CI is red."""
    return (
        f"⛔ gitops-deploy: CI is RED on `{origin[:8]}` — NOT deploying on {hostname}. "
        f"The host stays on `{local[:8]}` until master is green; fix forward or revert. "
        "(GitOps Status pages separately once the host has been behind for 6h.)"
    )
