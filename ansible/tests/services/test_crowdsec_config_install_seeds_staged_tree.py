"""Every `crowdsec-config-install` init container must run the entrypoint's own rsync.

The CrowdSec image entrypoint opens with a "Populating configuration directory" step —
`rsync -a --ignore-existing /staging/etc/crowdsec/* /etc/crowdsec` — that runs under `set -e`
and only while `/etc/crowdsec/config.yaml` is absent. About twenty staged files are root-only
(the LAPI and online credentials, the bundled hub tree), so the non-root agent sidecar exits
23 on it and the kubelet restarts the container. The second start finds `config.yaml` and
skips the block, so the pod settles at 2/2 Running with one restart on the clock.

That restart is not cosmetic: `probe.py health <svc>` fails closed on any container restart
inside its 180s window, so `land.sh` reported `VERDICT: unhealthy` for every authelia deploy
whose gate ran early (#1173). Traefik had already paid this (#976) and fixed it by running the
rsync in its init container; authelia's init container was never given the same line.

Nothing about this is visible from a passing render or from pod status — both pods read
healthy afterwards — so a guard over the rendered command text is the only cheap check. The
paired reject cases below matter as much as the accept ones: a predicate that fires on
everything and one that fires on nothing look identical from the passing side.

What this does NOT prove: that the seed actually succeeds in the image. That evidence is the
restart count on a live pod after a deploy.
"""

import sys

import pytest
from _helpers import REPO
from _k8s_render import rendered_docs

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

# Both pods carrying the sidecar. A rename or a third pod that drops the rsync must fail this
# rather than shrink the census to nothing and pass an all() over an empty set.
EXPECTED_ROLES = frozenset({"traefik", "authelia"})

INIT_NAME = "crowdsec-config-install"


def _seeds_the_staged_tree(command: str) -> bool:
    """True when the command pre-runs the entrypoint's rsync and tolerates ONLY exit 23.

    Exit 23 is partial transfer — the files it skips are ones the agent regenerates
    (`cscli lapi register`) or re-downloads (`cscli hub update`) on start. Any other status is
    a real seed failure and must fail the init container, so a blanket `|| true` is rejected:
    it would start an agent on a half-populated config with nothing saying so.
    """
    if "rsync" not in command or "/staging/etc/crowdsec" not in command:
        return False
    if "|| true" in command or "; true" in command:
        return False
    return '[ "$?" -eq 23 ]' in command


def _config_install_commands() -> dict[str, str]:
    found: dict[str, str] = {}
    for role, _template, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("initContainers") or []:
            if container["name"] == INIT_NAME:
                found[role] = " ".join(container["command"])
    return found


def test_every_pod_with_the_sidecar_seeds_the_staged_tree() -> None:
    commands = _config_install_commands()
    assert EXPECTED_ROLES <= set(commands), (
        f"no {INIT_NAME} init container rendered for "
        f"{sorted(EXPECTED_ROLES - set(commands))} — the census is empty for those roles, so "
        "the assertion below would pass while checking nothing"
    )
    for role, command in sorted(commands.items()):
        assert _seeds_the_staged_tree(command), (
            f"{role}'s {INIT_NAME} does not pre-run the entrypoint's rsync, so the agent "
            f"sidecar runs it, exits 23 and restarts once on every pod start (#1173)"
        )


def test_the_rendered_commands_are_accepted() -> None:
    """The accept half, stated separately so a predicate that stopped matching is visible."""
    commands = _config_install_commands()
    assert all(_seeds_the_staged_tree(c) for c in commands.values())


@pytest.mark.parametrize(
    ("label", "command"),
    [
        # Authelia's command as it stood before #1173: seeds only its own files.
        (
            "no rsync at all",
            "/bin/sh -c install -m 644 /seed/acquis.yaml /etc/crowdsec/ && "
            "install -d /etc/crowdsec/parsers/s02-enrich",
        ),
        # Tolerating every status hides a real seed failure behind a healthy-looking pod.
        (
            "blanket || true",
            "/bin/sh -c (rsync -a --ignore-existing /staging/etc/crowdsec/* /etc/crowdsec "
            "|| true) && install -m 644 /seed/acquis.yaml /etc/crowdsec/",
        ),
        # An rsync of something else does not populate the config directory.
        (
            "rsync of the wrong tree",
            '/bin/sh -c (rsync -a /seed/* /etc/crowdsec || [ "$?" -eq 23 ])',
        ),
    ],
)
def test_commands_that_leave_the_restart_in_place_are_rejected(
    label: str, command: str
) -> None:
    assert not _seeds_the_staged_tree(command), f"{label} should not have been accepted"
