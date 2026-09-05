"""Every pod carrying the crowdsec agent must stage the image's hub tree readably.

The image's staged `/staging/etc/crowdsec/hub` is root-only, and `crowdsec-config-install`
runs its rsync as the pod's own uid — that is exactly the exit 23 it tolerates. The parser
CONFIGS the rsync does copy are symlinks into that tree, so
`/etc/crowdsec/parsers/s02-enrich/geoip-enrich.yaml` resolves to a hub file that was never
staged and the agent drops the parser:

    level=warning msg="Ignoring file /etc/crowdsec/parsers/s02-enrich/geoip-enrich.yaml:
    lstat /etc/crowdsec/hub/parsers/s02-enrich/crowdsecurity/geoip-enrich.yaml: no such file
    or directory"

Measured on both pods on 2026-09-05 (#1211): six occurrences per authelia start, eight in
traefik's last 500 lines. GeoIP enrichment stays dead behind a 2/2 Running pod, which is why
a guard over the rendered command text is the only cheap check — nothing about it is visible
from pod status, and the datafiles half (#1177/#1208) reads green throughout.

The reject cases below are what proves the predicate can go red. Two of them are the shapes
most likely to be reached for by someone fixing this again: copying the tree without making
it readable (the mode is the whole defect), and widening the rsync's tolerance to `|| true`
instead, which the `DECIDED:` marker in the authelia template rules out.

What this does NOT prove: that the copy succeeds against the real image. That evidence is the
absence of the warning in a fresh pod's logs after a deploy.
"""

import pytest
from _k8s_render import rendered_docs

# Both pods carrying the sidecar. A rename, or a third pod that skips the hub staging, must
# fail this rather than shrink the census to nothing and pass an all() over an empty set.
EXPECTED_ROLES = frozenset({"traefik", "authelia"})

INIT_NAME = "crowdsec-hub-install"


def _stages_the_hub_readably(command: str) -> bool:
    """True when the command copies the staged hub tree AND makes it world-readable.

    Both halves are load-bearing. A copy that preserves the 0600 root:root staged modes
    leaves the non-root agent unable to read through the symlink, which is the same defect
    one directory over that #1177 fixed for the datafiles.
    """
    if "/staging/etc/crowdsec/hub" not in command:
        return False
    if "/etc/crowdsec/hub" not in command.replace("/staging/etc/crowdsec/hub", ""):
        return False
    return "chmod" in command and "a+rX" in command


def _hub_install_commands() -> dict[str, str]:
    found: dict[str, str] = {}
    for role, _template, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("initContainers") or []:
            if container["name"] == INIT_NAME:
                found[role] = " ".join(container["command"])
    return found


def test_every_pod_with_the_sidecar_stages_the_hub_tree() -> None:
    commands = _hub_install_commands()
    assert EXPECTED_ROLES <= set(commands), (
        f"no {INIT_NAME} init container rendered for "
        f"{sorted(EXPECTED_ROLES - set(commands))} — the census is empty for those roles, so "
        "the assertion below would pass while checking nothing"
    )
    for role, command in sorted(commands.items()):
        assert _stages_the_hub_readably(command), (
            f"{role}'s {INIT_NAME} does not stage the hub tree world-readable, so every "
            f"parser config that symlinks into it is dropped at load (#1211)"
        )


def test_the_hub_install_runs_as_root_with_dac_read_search() -> None:
    """The staged sources are root-only, so a non-root copy reads nothing to copy."""
    seen: set[str] = set()
    for role, _template, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("initContainers") or []:
            if container["name"] != INIT_NAME:
                continue
            seen.add(role)
            security = container["securityContext"]
            assert security["runAsUser"] == 0, f"{role}'s {INIT_NAME} must run as root"
            assert "DAC_READ_SEARCH" in security["capabilities"]["add"], (
                f"{role}'s {INIT_NAME} drops ALL capabilities, so root without "
                "DAC_READ_SEARCH cannot read the 0600 staged hub files"
            )
    assert EXPECTED_ROLES <= seen, f"census missing {sorted(EXPECTED_ROLES - seen)}"


def test_the_hub_install_cannot_fail_the_pod() -> None:
    """Both pods roll under Recreate: a non-zero init container is an outage, not a parser."""
    commands = _hub_install_commands()
    assert EXPECTED_ROLES <= set(commands), f"census missing {sorted(EXPECTED_ROLES)}"
    for role, command in sorted(commands.items()):
        assert command.rstrip().endswith("exit 0"), (
            f"{role}'s {INIT_NAME} can exit non-zero, which takes the pod down under "
            "Recreate rather than costing one parser"
        )


def test_the_hub_install_runs_before_the_config_install() -> None:
    """Order is load-bearing, not cosmetic.

    `crowdsec-config-install`'s rsync recurses from the parent directory listing, so it
    creates `/etc/crowdsec/hub` owned by the pod's own uid before it fails to read into it.
    Root with ALL dropped cannot then write into another uid's directory — DAC_READ_SEARCH is
    the read half of the permission-bit override, DAC_OVERRIDE is the write half. The copy
    would fail, the trailing `exit 0` would swallow it, and the pod would come up 2/2 with the
    warning intact. Going first, the hub install creates the directory itself, root-owned and
    world-readable, in an emptyDir that is still empty.
    """
    seen: set[str] = set()
    for role, _template, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        names = [
            c["name"]
            for c in doc["spec"]["template"]["spec"].get("initContainers") or []
        ]
        if INIT_NAME not in names or "crowdsec-config-install" not in names:
            continue
        seen.add(role)
        assert names.index(INIT_NAME) < names.index("crowdsec-config-install"), (
            f"{role} runs {INIT_NAME} after the rsync, which leaves /etc/crowdsec/hub owned "
            "by the pod uid and unwritable by root-without-DAC_OVERRIDE — the copy fails "
            "silently behind exit 0"
        )
    assert EXPECTED_ROLES <= seen, f"census missing {sorted(EXPECTED_ROLES - seen)}"


def test_the_rendered_commands_are_accepted() -> None:
    """The accept half, stated separately so a predicate that stopped matching is visible."""
    commands = _hub_install_commands()
    assert commands
    assert all(_stages_the_hub_readably(c) for c in commands.values())


@pytest.mark.parametrize(
    ("label", "command"),
    [
        (
            "no hub staging at all",
            "/bin/sh -c install -m 644 /seed/acquis.yaml /etc/crowdsec/",
        ),
        (
            "copies the tree but keeps the staged 0600 modes",
            "/bin/sh -c mkdir -p /etc/crowdsec/hub && "
            "cp -a /staging/etc/crowdsec/hub/. /etc/crowdsec/hub/; exit 0",
        ),
        (
            "chmods a tree it never copied",
            "/bin/sh -c chmod -R a+rX /etc/crowdsec/hub; exit 0",
        ),
        (
            "widens the rsync tolerance instead of staging the hub",
            "/bin/sh -c rsync -a --ignore-existing /staging/etc/crowdsec/* "
            "/etc/crowdsec || true",
        ),
    ],
)
def test_commands_that_leave_the_parser_dead_are_rejected(
    label: str, command: str
) -> None:
    assert not _stages_the_hub_readably(command), label
