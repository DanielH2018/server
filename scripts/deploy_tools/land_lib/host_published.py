"""Whether the builder has published a host's package at the merge commit or after it (#7024).

`host_wait.wait_for_hosts` asks before it ends a landing `deferred` on a host's updater. An
updater fire installs only what the builder has published, so a deferral on a host whose
package was never built at the merge names a fire that cannot apply it. During the builder
outage of 2026-11-02, five landings read `deferred` that way and hid the outage.

It reads the two series `probe.py builder` reads from daniel-box's textfiles, through the
Prometheus query the host wait already uses:

- `homelab_repo_published_info{package,sha,version}`: each version the repository serves;
- `homelab_builder_unbuilt_since_timestamp_seconds{reason,sha}`: the builder's reason.

Typical usage example (`host_wait.py`'s, before it ends a landing `deferred`):

    unbuilt = unbuilt_hosts(["daniel-pi"], merge, primary, reads, covers, query)
"""

import sys as _sys
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.outcome import say
from diagnostics.probe_lib.builder import HEALTHY

PUBLISHED = "homelab_repo_published_info"
UNBUILT_SINCE = "homelab_builder_unbuilt_since_timestamp_seconds"
# The reasons of a builder that publishes the merge without a hand. `no-green` is one because
# the host wait runs only once master CI is green at the merge: the builder has not run since.
LAGGING = HEALTHY | {"no-green"}


def package_of(host: str) -> str:
    """The host package's name, as `renderer_lib/host.py` names it."""
    return f"homelab-{host}"


def unbuilt_hosts(
    hosts: list[str],
    merge: str,
    primary: Path,
    reads,
    covers,
    query,
) -> list[str]:
    """One sentence for each of `hosts` whose package the builder has not published at `merge`.

    A host counts only while the builder's reason is outside `LAGGING`.

    DECIDED: an `ok`, `canary` or `no-green` builder that has not reached the merge is lagging
    by design: its timer runs every 2 minutes, a canary gate holds the other hosts until the
    canary applies, and `no-green` predates the green CI the landing waited on. Those landings
    stay `deferred`. Any other reason means no build is coming.

    Args:
        hosts: The hosts the landing would defer on.
        merge: The merge commit's full SHA.
        primary: The checkout `covers` asks git in.
        reads: `host_wait.HostReads`, passed through to `covers` and `query`.
        covers: `host_wait.covers`.
        query: `host_wait.query`.

    Returns:
        An empty list when every host's package is published at or after `merge`, when the
        builder is healthy, or when a series cannot be read: an unread source proves nothing.
    """
    try:
        published = [s["metric"] for s in query(PUBLISHED, reads)]
        builder = [s["metric"] for s in query(UNBUILT_SINCE, reads)]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        say(f"the builder's published versions could not be read ({exc}); deferring")
        return []
    if not builder:
        say(f"Prometheus reports no {UNBUILT_SINCE}; deferring")
        return []
    reason = builder[0].get("reason", "?")
    if reason in LAGGING:
        return []
    sentences = []
    for host in hosts:
        package = package_of(host)
        rows = [labels for labels in published if labels.get("package") == package]
        if not rows:
            say(f"Prometheus reports no {PUBLISHED} for {package}; deferring {host}")
            continue
        if any(covers(r.get("sha", ""), merge, primary, reads) for r in rows):
            continue
        versions = ", ".join(sorted(r.get("version", "?") for r in rows))
        sentences.append(
            f"the builder has published no {package} at or after {merge[:12]}, so no "
            f"updater fire on {host} can apply it: the builder's reason is `{reason}`, "
            f"and the last {package} it published is {versions}. Read "
            "`uv run python scripts/diagnostics/probe.py builder`, fix the builder, then "
            "re-run land.sh"
        )
    return sentences
