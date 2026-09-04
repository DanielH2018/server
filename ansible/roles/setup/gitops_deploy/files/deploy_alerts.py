# ansible/roles/setup/gitops_deploy/files/deploy_alerts.py
"""The deployer's notify subsystem: the webhook, the queue's file, and every message body.

Splitting this out separates two things that were interleaved inside `main()`. **What to say**
is here, as one named pure function per alert — so the text is testable without driving a tick,
and a 1900-character budget can be asserted against the assembled post rather than guessed at.
**When to say it** stays in `gitops_deploy.py`: `alert_once` owns the per-SHA dedupe marker and
`deliver` owns the retry queue, and the test suite drives both by patching them there.

`deploy_health.py` already held the queue's pure half (`apply_send_result`, `cap_pending`,
`apply_drain_result`); this module is its I/O counterpart plus the composers.

Reach these functions qualified (`deploy_alerts.post(...)`), never by from-import —
see `deploy_io.py`'s docstring for why.
"""

import json

from deploy_config import log
from deploy_failtext import failing_task, head, tail
from deploy_remediation import k8s_remediation
from host_lib import atomic_write, discord_post

# Per-alert budget for an embedded error string. host_lib.discord_post cuts a post at
# `message[:1900]`, keeping the HEAD — so an unbounded error string does not truncate itself, it
# evicts the remediation prose that follows it. Sized for the longest of the three failure posts.
ALERT_EXCERPT_CHARS = 700


# ── the webhook, and the queue's file ─────────────────────────────────────────────────────────


def post(webhook: str, content: str, log_fn=log) -> bool:
    """Post to the alert webhook via the shared host_lib.discord_post.

    See there for the Cloudflare-1010 User-Agent + 2xx-only-success contract the per-SHA
    dedupe markers gate on. A missing webhook or any error returns False, so the alert is
    retried on the next tick.
    """
    return discord_post(webhook, content, "gitops-deploy", log=log_fn)


def read_pending(path: str) -> dict[str, str]:
    """The queued-but-undelivered alerts, or {} when the file is absent or unreadable as JSON."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    # Split (not `except (A, B)`): ruff (3.14 target, from requires-python) reformats a
    # parenthesized tuple into the 3.14-only `except A, B:` form. Two clauses give ruff nothing
    # to rewrite. Still load-bearing: unlike its siblings this unit has NOT yet moved to the
    # pinned 3.14 (docs/archive/host-python-314-plan.md, task 6), so it runs on the host's 3.12 today and
    # the rewritten form would SyntaxError. Keep the split until that task lands.
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def write_pending(path: str, pending: dict[str, str]) -> None:
    """Persist the queue. Atomic (temp + rename, see host_lib): a torn write mustn't strand it."""
    atomic_write(path, json.dumps(pending))


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
        f"No tick ran. Re-render it with `uv run ansible-playbook "
        f"ansible/initial_setup.yml --tags gitops_deploy`."
    )


def broad_failure_alert(
    hostname: str,
    playbook: str,
    tags: list[str],
    origin: str,
    exc: BaseException,
    hold_file: str,
    hold_plane_file: str,
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
        f"**Action:** fix forward and re-run that playbook by hand. A later tick clears the "
        f"hold only by applying this same plane — after a hand run, "
        f"`rm {hold_file} {hold_plane_file}`."
    )


def broad_deferred_alert(origin: str, remediation: str) -> str:
    """The post for a broad change this deployer will not apply itself."""
    return (
        f"⚠️ gitops-deploy: broad change needing a hand in `{origin[:8]}` — "
        f"deferring to a manual deploy. On the host, run "
        f"{remediation} "
        f"to clear it."
    )


def secrets_deferred_alert(origin: str) -> str:
    """The post for a `secrets.yml` change that ff-merged with no consumer redeployed."""
    return (
        f"⚠️ gitops-deploy: `secrets.yml` changed in `{origin[:8]}` with no "
        f"service template — fast-forwarded but **nothing was redeployed**. The "
        f"rotated secret won't reach its container(s) until you redeploy them "
        f"(`ansible-playbook ansible/deploy.yml --tags <svc>`)."
    )


def tasks_deferred_alert(origin: str, services: set[str]) -> str:
    """The post for a structural-dir change to services this tick did not redeploy."""
    return (
        f"⚠️ gitops-deploy: a structural dir (`tasks/`/`defaults/`/`vars/`/`handlers/`) changed "
        f"for `{', '.join(sorted(services))}` in `{origin[:8]}` with no redeploy of those "
        f"service(s) — fast-forwarded but **not applied** (those dirs aren't auto-deployed). "
        f"Redeploy by hand: `ansible-playbook ansible/deploy.yml --tags <svc>`."
    )


def meta_deferred_alert(origin: str, services: set[str]) -> str:
    """The post for a `meta/deps.yml` change to services this tick did not redeploy."""
    return (
        f"⚠️ gitops-deploy: `meta/deps.yml` changed for "
        f"`{', '.join(sorted(services))}` in `{origin[:8]}` with no redeploy of those "
        f"service(s) — fast-forwarded but **not applied** (meta/ isn't auto-deployed; it "
        f"changes deploy ordering + dep closure). Redeploy the affected service(s) by hand: "
        f"`ansible-playbook ansible/deploy.yml --tags <svc>`."
    )


def k8s_deferred_alert(
    origin: str, k8s: set[str], declared_k8s: set[str], consumers: set[str] | None
) -> str:
    """The post for a k8s role change, which this deployer never auto-deploys.

    The remediation half is `deploy_remediation.k8s_remediation`, which decides whether the
    change can be named as a `--tags` redeploy at all.
    """
    return (
        f"⚠️ gitops-deploy: k8s role(s) `{', '.join(sorted(k8s))}` changed in "
        f"`{origin[:8]}` — fast-forwarded but **not applied** (this deployer only "
        f"auto-deploys Docker-platform services; k8s roles are defer-and-alert). "
    ) + k8s_remediation(k8s, declared_k8s, consumers)


def stale_composes_alert(hostname: str, stale: list[str]) -> str:
    """The post for a rendered compose with no containers_list entry."""
    return (
        f"⚠️ gitops-deploy: stale rendered compose(s) on {hostname} with no "
        f"containers_list entry: `{', '.join(stale)}` — a retired/migrated service "
        f"left its render behind, and its phantom containers will fail the health "
        f"gate on that service's next deploy (false rollback + hold). Clean up: "
        f"`docker rm -f <its containers>` then `rm -rf containers/<svc>`."
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


def stale_denylist_alert(origin: str, detail: str, fix: str) -> str:
    """The post for a config.env denylist that disagrees with the declarations at origin."""
    return (
        f"⚠️ gitops-deploy: `/etc/gitops-deploy/config.env` denylist is stale against "
        f"`{origin[:8]}` — {detail}. k8s auto-deploy is DISARMED until this is fixed: "
        f"{fix}."
    )


def staging_verdict_alert(origin: str, summary: str, blocking: bool) -> str:
    """The post for any staging verdict that is not a PASS."""
    tail_line = (
        "This gate BLOCKS — prod was not deployed unless the verdict was no_verdict."
        if blocking
        else "Prod deployed regardless — this gate does not block yet."
    )
    return f"🧪 gitops-deploy: {summary} for `{origin[:8]}`. {tail_line}"


def staging_override_alert(hostname: str, origin: str, override_file: str) -> str:
    """The post announcing that the operator's one-tick override was spent."""
    return (
        f"🔓 gitops-deploy: **staging override used** on {hostname}. "
        f"Staging REJECTED `{origin[:8]}` and it was deployed to prod anyway, "
        f"because `{override_file}` was armed.\n"
        f"The override is now spent — re-arm it with `touch` if the next tick "
        f"needs it too."
    )


def staging_rejected_alert(
    hostname: str, local: str, origin: str, services: set[str], override_file: str
) -> str:
    """The post for a blocking staging rejection: nothing was applied, nothing to undo."""
    return (
        f"🧪 gitops-deploy: **staging REJECTED** `{origin[:8]}` on {hostname} — "
        f"prod was NOT deployed and the tree stays on `{local[:8]}`.\n"
        f"`{', '.join(sorted(services))}` failed on daniel-stage. Nothing was "
        f"applied here, so there is nothing to roll back and no volume was "
        f"reverted.\n"
        f"**Action:** fix forward on master, or — if staging itself is the "
        f"problem rather than the change — `touch {override_file}` to let "
        f"the next tick through once.\n"
        f"The hold only skips THIS commit: `skip_hold` matches while "
        f"`origin_head == hold_sha`, so the next push past it is gated afresh."
    )


def k8s_failure_alert(
    hostname: str,
    local: str,
    origin: str,
    services: set[str],
    exc: BaseException,
    revert_note: str,
) -> str:
    """The post for a k8s deploy that failed its rollout gate and was rolled back."""
    return (
        f"🚨 gitops-deploy: **k8s deploy failed** on {hostname}.\n"
        f"`{', '.join(sorted(services))}` from `{origin[:8]}` failed its rollout "
        f"gate:\n```\n{alert_excerpt(exc)}\n```\n"
        f"Rolled back locally to `{local[:8]}`.\n"
        f"{revert_note}"
        f"**The bad pin is still live on master.** The hold only skips THIS commit — "
        f"`skip_hold` matches while `origin_head == hold_sha`, so the next push past it "
        f"redeploys the same pin.\n"
        f"**Action:** revert the offending commit on the remote, or pin the bad version "
        f"out via Renovate `allowedVersions`."
    )


def deploy_failure_alert(
    hostname: str, local: str, origin: str, services: set[str], exc: BaseException
) -> str:
    """The post for an `ansible-playbook` that errored deploying Docker services."""
    return (
        f"🚨 gitops-deploy: **deploy failed** on {hostname}.\n"
        f"`ansible-playbook` errored deploying `{', '.join(sorted(services))}` from "
        f"`{origin[:8]}`:\n```\n{alert_excerpt(exc)}\n```\n"
        f"Rolled back to `{local[:8]}`; the bad commit is held until origin advances past it.\n"
        f"**Action:** fix or revert the offending commit."
    )


def rollback_alert(hostname: str, local: str, origin: str, failed: list[str]) -> str:
    """The post for a Docker health gate that failed and rolled the host back."""
    return (
        f"🚨 gitops-deploy: **rollback** on {hostname}.\n"
        f"Service(s) `{', '.join(failed)}` from commit `{origin[:8]}` failed the health "
        f"gate and were rolled back to `{local[:8]}`.\n"
        f"**Action:** revert the offending Renovate PR — the bad commit is held until you do."
    )
