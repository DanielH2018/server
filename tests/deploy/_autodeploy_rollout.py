"""Is a Deployment or DaemonSet credited as gated -- the rollout half of the derivation.

A role's primary rollout is gated by its `manifests_rollout` entry; every other workload it renders
must be named in `manifests_extra_rollouts` or it deploys ungated.
Consumed by `test_k8s_autodeploy_rollout_gates.py`.
"""

import re
from pathlib import Path

from _autodeploy import (
    _LITERAL_NAME,
    _deployment_templates,
    _template_texts,
)


def _render_metadata_text(role: Path) -> str:
    """`role`'s defaults/main.yml, the file a render key sits in, or "" for a role with none."""
    defaults = role / "defaults/main.yml"
    return defaults.read_text() if defaults.is_file() else ""


_EXTRA_ROLLOUTS_BLOCK = re.compile(
    r"^[ \t]*manifests_extra_rollouts:[ \t]*(?:#.*)?\n"
    r"(?P<indent>[ \t]*)-[ \t]*name:.*\n?"
    r"(?:(?P=indent)(?:-[ \t]*name:|[ \t]+\S).*\n?)*",
    re.MULTILINE,
)


def _extra_rollouts(role: Path) -> set[str]:
    """Deployment names the role gates via `manifests_extra_rollouts`.

    Parsed with a regex rather than yaml.safe_load: the value may be Jinja-templated, and a
    role is free to build the list from a variable. A name this cannot see reads as ungated,
    which fails the guard — the safe direction.

    The first `- name:` fixes the list's indent. A line at that indent starting `- name:` is
    the next entry, and a line indented deeper is a key of the entry above it (`restart_on:
    []`, `kind: daemonset`), so an entry carrying a second key hides none listed after it.
    A line at the list's indent or shallower that is not `- name:` ends the list,
    which is where a sibling var such as `manifests_rollout_timeout:` sits. Its own list is
    never credited.
    """
    block = _EXTRA_ROLLOUTS_BLOCK.search(_render_metadata_text(role))
    if not block:
        return set()
    item = re.escape(block.group("indent"))
    return set(
        re.findall(rf"^{item}-[ \t]*name:[ \t]*(\S+)", block.group(0), re.MULTILINE)
    )


def _primary_rollout_name(role: Path) -> str:
    """The Deployment name this role declares as its primary rollout.

    Reads `manifests_rollout | default(manifests_service)`. `manifests_service` defaults to the role directory's
    name, a caller that overrides it writes a literal string, and every
    `manifests_rollout` override is likewise a literal (checked repo-wide), so a regex match is
    safe here. A role that sets neither key renders under its own name.

    Tolerates a trailing comment after the value: a match anchored at end-of-line right after
    the closing quote would read `manifests_rollout: ''  # nothing to roll` as unset, fall
    through to `manifests_service` and return the real service name.
    """
    text = _render_metadata_text(role)
    rollout = re.search(
        r"""^\s*manifests_rollout:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*(?:#.*)?$""",
        text,
        re.MULTILINE,
    )
    if rollout:
        return next(g for g in rollout.groups() if g is not None)
    service = re.search(r"^\s*manifests_service:\s*(\S+)\s*$", text, re.MULTILINE)
    if service:
        return service.group(1)
    return role.name


_DEPLOYMENT_DOC = re.compile(
    r"^kind:\s*(Deployment|DaemonSet)\s*$\n\s*metadata:\s*$\n\s*name:\s*(.+?)\s*$",
    re.MULTILINE,
)


def _deployment_docs(text: str) -> list[tuple[str, str | None]]:
    """Every Deployment or DaemonSet in a rendered template, as (kind, metadata.name).

    Every Deployment template puts `name:` two lines under `kind: Deployment` (`metadata:` in
    between). One template can render several `---`-separated workloads, so every match is
    returned: a guard reading only the first would wave the rest through. A name that is not a
    static literal comes back as None, which the callers treat as ungated, the fail-closed
    direction; on a render that is a value the render left unresolved.
    """
    return [
        (kind, name if _LITERAL_NAME.match(name) else None)
        for kind, name in _DEPLOYMENT_DOC.findall(text)
    ]


def _gated_names(role: Path) -> set[str]:
    """Deployment names this role's rollout gate actually waits on."""
    return {_primary_rollout_name(role)} | _extra_rollouts(role)


def _ungated_deployments(role: Path) -> list[str]:
    """Rendered Deployment templates whose resolved name is not in the gated set.

    Matches by identity, not count: a rendered Deployment is gated only if its own resolved
    name equals the primary rollout name or appears in manifests_extra_rollouts. A typo'd or
    drifted extra name, or a Deployment whose name can't be resolved statically, both count as
    ungated. A count alone would let a mismatched name through as long as the totals lined up.
    """
    gated = _gated_names(role)
    texts = _template_texts(role)
    out = []
    for template in _deployment_templates(role):
        docs = _deployment_docs(texts[template])
        # No doc parsed means `_deployment_templates` saw a quoted or commented `kind:` this
        # strict pattern cannot pair with a name, so nothing proves it gated.
        if not docs or any(name not in gated for _, name in docs):
            out.append(template)
    return out


def _ungated_deployment_count(role: Path) -> int:
    return len(_ungated_deployments(role))


_MANIFEST_KIND_TO_ROLLOUT_KIND = {"Deployment": "deploy", "DaemonSet": "daemonset"}


def _deployments_missing_readiness_probe(role: Path) -> list[str]:
    """Rendered Deployment template names with no readinessProbe.

    Checks every rendered Deployment individually. An `any()` across the role's templates would
    let a probe on the primary Deployment satisfy the whole role and a probe-less *extra* pass
    unchecked.
    """
    texts = _template_texts(role)
    return [
        name
        for name in _deployment_templates(role)
        if "readinessProbe" not in texts[name]
    ]
