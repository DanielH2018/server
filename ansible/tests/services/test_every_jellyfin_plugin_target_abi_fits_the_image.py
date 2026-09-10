#!/usr/bin/env python3
"""`jellyfin_k8s_image` must satisfy the targetAbi of EVERY plugin the role installs.

Each per-plugin guard beside this one checks their own pin against the image. None of
them checks the SET, and the set is what the role's lockstep rule is about: the image cannot move
to a new Jellyfin line until every installed plugin has a release for that ABI, so the binding
constraint is `max` over the declared `targetAbi` values — the slowest plugin, not the newest.

Two gaps that leaves, and this file closes both:

- **A sixth plugin added without its own guard is unchecked.** The census in
  `test_webhook_plugin_install.py` fails when an installer appears with no guard, but nothing
  reads a new `_target_abi` var against the image. This test finds its subjects by GLOBBING the
  vars, so a new one is covered the moment it is written.
- **The floor was wrong in prose.** The role's CLAUDE.md claimed Media Cleaner's `10.11.9.0` was
  the tightest declared; it is not — ani-sync and Intro Skipper both declare `10.11.11.0`,
  which is why the image sits at `10.11.11`. A sentence a reader consults before bumping the
  image should not be the thing that decides it, so the floor is derived here instead.

A glob-based census returns an empty set the moment the vars are renamed, and `all(...)` over
nothing passes — so `REQUIRED_PLUGINS` names members this must find. That is the non-vacuity
half, and it is what a red-proof pair alone would not catch.

Run: uv run pytest ansible/tests/services/test_every_jellyfin_plugin_target_abi_fits_the_image.py
"""

import re

import pytest

from lib import yaml_fast
from _helpers import ANSIBLE

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"

TARGET_ABI_VAR = re.compile(r"^jellyfin_k8s_(?P<plugin>[a-z0-9]+)_target_abi$")
LEADING_VERSION = re.compile(r"^(\d+(?:\.\d+)*)")

# The members the census MUST contain. A rename breaks this loudly rather than emptying the set.
# ani-sync is absent on purpose: it declares no `_target_abi` var — its ABI leads the release
# asset's filename, and `test_anisync_pin_matches_server.py` owns that shape. It is read here
# from the URL so the `max` below is over EVERY plugin rather than only those with a var.
REQUIRED_PLUGINS = frozenset(
    {"introskipper", "webhook", "mergeversions", "mediacleaner", "sso"}
)


def _defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def _version_tuple(text: str, what: str) -> tuple[int, ...]:
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple, tuple]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _declared_abis(defaults: dict) -> dict[str, tuple[int, ...]]:
    """Every plugin's targetAbi, keyed by plugin slug.

    Globbed from the vars rather than listed, so a plugin added later is covered without anyone
    remembering to extend this file. ani-sync is added from its asset filename.
    """
    found = {}
    for name, value in defaults.items():
        match = TARGET_ABI_VAR.match(name)
        if match:
            found[match.group("plugin")] = _version_tuple(str(value), name)

    asset = defaults["jellyfin_k8s_anisync_url"].rsplit("/", 1)[-1]
    found["anisync"] = _version_tuple(asset, "the ani-sync release asset filename")

    # NORMALISED TO ONE WIDTH before returning, and this is load-bearing for `max`. The vars are
    # four-part (`10.11.11.0`) while ani-sync's ABI comes off an asset filename and parses to
    # three (`10.11.11`). Python compares tuples element-wise and a prefix loses, so
    # (10, 11, 11) < (10, 11, 11, 0) — two SEMANTICALLY EQUAL values. Unnormalised, `max` picks
    # the longer one and the holders list silently omits ani-sync, which is how the prose this
    # test replaced came to name the wrong plugin.
    width = max(len(abi) for abi in found.values())
    return {plugin: abi + (0,) * (width - len(abi)) for plugin, abi in found.items()}


def test_the_census_still_finds_every_plugin_it_is_meant_to():
    """Non-vacuity. A glob that matches nothing makes every assertion below pass."""
    found = set(_declared_abis(_defaults()))

    missing = REQUIRED_PLUGINS - found
    assert not missing, (
        f"no jellyfin_k8s_<plugin>_target_abi var found for {sorted(missing)}. This census is "
        f"globbed, so a renamed var silently shrinks it and every check below would pass over "
        f"a smaller set. Either the var was renamed — update REQUIRED_PLUGINS with it — or the "
        f"plugin was removed, in which case drop it from REQUIRED_PLUGINS deliberately."
    )
    assert "anisync" in found, (
        "ani-sync's targetAbi could not be read from jellyfin_k8s_anisync_url. It has no "
        "_target_abi var — the ABI leads the asset filename — so a URL shape change drops it "
        "from the max() below and the binding constraint would be computed over one plugin too few."
    )
    assert len(found) >= 6, (
        f"the targetAbi census found only {sorted(found)}. The role installs six plugins; a "
        f"smaller set means the max() below is not the real floor."
    )


def test_the_image_satisfies_every_plugin_target_abi():
    """The lockstep rule, as an invariant over the SET rather than per plugin.

    Jellyfin's loader rejects a plugin built for a newer server SILENTLY — the directory sits on
    disk, `GET /Plugins` omits it, the rollout stays green. So a plugin whose targetAbi exceeds
    the image is not a deploy failure, it is a plugin nobody notices is gone.
    """
    defaults = _defaults()
    abis = _declared_abis(defaults)
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    too_new = {}
    for plugin, abi in abis.items():
        left, right = _padded(abi, server)
        if left > right:
            too_new[plugin] = ".".join(map(str, abi))

    assert not too_new, (
        f"these plugins target a Jellyfin newer than jellyfin_k8s_image ({image}): "
        f"{too_new}.\nThe loader rejects each of them without logging a failure, so the pod "
        f"comes up healthy with the plugin absent. Raise the image, or take a release on the "
        f"line the image satisfies."
    )


def test_the_binding_floor_is_the_slowest_plugin():
    """States WHICH plugin pins the image, derived rather than written in prose.

    The role's CLAUDE.md carried this as a sentence and the sentence was wrong — it named Media
    Cleaner's 10.11.9.0, where the real floor is ani-sync's and Intro Skipper's 10.11.11.0. A
    reader consults that before bumping the image, so the claim belongs somewhere a rename or a
    new plugin moves it automatically.
    """
    defaults = _defaults()
    abis = _declared_abis(defaults)
    floor = max(abis.values())
    holders = sorted(p for p, abi in abis.items() if abi == floor)
    server = _version_tuple(
        defaults["jellyfin_k8s_image"].rsplit(":", 1)[-1], "the jellyfin image tag"
    )

    left, right = _padded(floor, server)
    assert left <= right, (
        f"the binding targetAbi floor is {'.'.join(map(str, floor))} (held by {holders}), which "
        f"jellyfin_k8s_image {defaults['jellyfin_k8s_image']} does not satisfy"
    )
    # The floor must be a real constraint, not a formatting artefact: at least one plugin has to
    # sit AT it, or `max` was computed over an empty or malformed set.
    assert holders, "no plugin holds the computed floor — the census is malformed"

    # NAMED, because this is the claim the role's CLAUDE.md makes in prose and got wrong. An
    # assertion on the computed value alone would pass whichever plugin held the floor, so it
    # could not have caught that error. When a plugin bump moves the floor, this fails and names
    # the new holders — update both this set and the CLAUDE.md sentence together.
    assert set(holders) == {"anisync", "introskipper"}, (
        f"the binding targetAbi floor is {'.'.join(map(str, floor))}, now held by {holders} "
        f"rather than ani-sync and Intro Skipper. That is the constraint on jellyfin_k8s_image, "
        f"and the role's CLAUDE.md states it in prose — update the sentence in its *At a glance* "
        f"section and this set together, or the doc a reader consults before bumping the image "
        f"names the wrong plugin."
    )


@pytest.mark.parametrize(
    ("what", "before", "after"),
    [
        # NAMED with its var, not the bare ABI string: `"10.11.9.0"` alone matches both
        # mediacleaner's and trakt's pin while Trakt was installed (#1617), and `.replace(..., 1)`
        # would silently mutate whichever came first. Kept named so a new sibling cannot reopen it.
        (
            "a plugin targeting a newer server",
            'jellyfin_k8s_mediacleaner_target_abi: "10.11.9.0"',
            'jellyfin_k8s_mediacleaner_target_abi: "10.12.0.0"',
        ),
        (
            "an image behind a plugin's ABI",
            "jellyfin:10.11.11ubu2604",
            "jellyfin:10.11.7ubu2604",
        ),
    ],
)
def test_the_guard_rejects_a_mismatched_defaults_file(what, before, after):
    """The red half, run against a mutated COPY of the real defaults.

    Both mutations are states a careless bump really produces, and neither fails a deploy — which
    is the whole reason this check exists.
    """
    text = DEFAULTS.read_text()
    assert before in text, f"the mutation for {what} matched nothing — fix the fixture"
    defaults = yaml_fast.safe_load(text.replace(before, after, 1))

    abis = _declared_abis(defaults)
    server = _version_tuple(
        defaults["jellyfin_k8s_image"].rsplit(":", 1)[-1], "the jellyfin image tag"
    )
    left, right = _padded(max(abis.values()), server)
    assert left > right, (
        f"{what} left every targetAbi still satisfied by the image, so the real guard would pass "
        f"on it — the mutation does not exercise what it claims to"
    )


def test_the_census_rejects_a_renamed_var():
    """The red half for non-vacuity, which the pair above cannot reach.

    A glob census that stops matching goes EMPTY, and both halves of an ordinary red-proof pair
    still pass on an empty set — they are only ever handed inputs that fire. This is the failure
    mode nine guards in this repo shipped with.
    """
    mutated = yaml_fast.safe_load(
        DEFAULTS.read_text().replace(
            "jellyfin_k8s_mediacleaner_target_abi:",
            "jellyfin_k8s_mediacleaner_abi_target:",
            1,
        )
    )
    assert "mediacleaner" not in _declared_abis(mutated), (
        "renaming jellyfin_k8s_mediacleaner_target_abi left it in the census — the regex is "
        "matching something other than the var name it documents"
    )
