"""The cast connect-back verify signal must sit on a logger `configuration.yaml` leaves alone.

Issue #2781 set `pychromecast.controllers` to `critical` so the Nest Hub Max's cast
connect-back tracebacks stopped holding monitor-bridge's composite `k8s_workloads` tile red.
That logger carries BOTH records the fault emits — the `_connect_hass failed` WARNING and the
`Exception thrown when calling cast status listener` ERROR — so after #2781 a query for either
one returns nothing whether or not the fault is firing. Issue #2800's verify-by was written
against `_connect_hass failed` and was therefore unfalsifiable on the deployed config.

The channel that survives is the Hub Max's receiver posting its own JS exceptions back to HA,
which HA logs under `frontend.js.*`. `docs/platform.md` names that as the only usable signal.
It stays usable only while nothing silences it, and a future `logger:` entry could take it away
as quietly as #2781 took the first one — silently, behind a green deploy, with the docs still
pointing an operator at a query that cannot fire.

HA's logger integration applies the LONGEST matching dotted prefix, so `frontend: critical` and
`frontend.js: fatal` suppress `frontend.js.modern.<build>` just as surely as naming it exactly.
The predicate below walks prefixes for that reason, with a passing and a rejecting input each,
then runs against the real `files/configuration.yaml`.
"""

import pytest
import yaml
from _helpers import REPO

HA_CONFIG = REPO / "ansible/roles/k8s/home-assistant/files/configuration.yaml"
PLATFORM_DOC = REPO / "ansible/roles/k8s/home-assistant/docs/platform.md"

# The logger `docs/platform.md` tells an operator to query. Its records are the cast receiver's
# own JS exceptions, which is why they survive a `pychromecast.controllers` silence.
VERIFY_LOGGER = "frontend.js.modern"

# Levels at which HA drops an ERROR record. `error` still lets an ERROR through, so it is not
# here; `warning` and below are louder still.
SILENT_LEVELS = frozenset({"critical", "fatal", "none"})

# The silence issue #2781 installed. Named so this guard fails loudly if that entry is dropped:
# the docs explain the verify signal by reference to it, and an un-silenced controller logger
# would make them wrong in the other direction.
SILENCED_BY_2781 = "pychromecast.controllers"


def _ha_logger_block() -> dict:
    """The `logger:` mapping out of the shipped configuration.yaml.

    `configuration.yaml` carries HA tags (`!secret`, `!include`) that `yaml.safe_load` rejects,
    so unknown tags resolve to None. The `logger:` block uses none of them.
    """

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    config = yaml.load(HA_CONFIG.read_text(), Loader=_Loader)
    return config["logger"]


def silencers_of(logger_block: dict, logger_name: str) -> set[str]:
    """Entries in a `logger:` block that stop `logger_name` emitting an ERROR record.

    Returns the offending keys rather than a bool so a failure names what to look at. `default`
    counts: it is the level every logger with no more specific entry inherits.
    """
    logs = logger_block.get("logs") or {}
    prefixes = set()
    parts = logger_name.split(".")
    for depth in range(1, len(parts) + 1):
        prefixes.add(".".join(parts[:depth]))

    offenders = {
        name
        for name, level in logs.items()
        if name in prefixes and level in SILENT_LEVELS
    }
    if (
        not any(name in logs for name in prefixes)
        and logger_block.get("default") in SILENT_LEVELS
    ):
        offenders.add("default")
    return offenders


@pytest.mark.parametrize(
    "logger_block",
    [
        {"default": "info", "logs": {"pychromecast.controllers": "critical"}},
        {"default": "info", "logs": {"frontend.js.modern": "warning"}},
        {"default": "info", "logs": {"frontend": "debug", "frontend.js": "info"}},
        {"default": "info", "logs": {}},
    ],
)
def test_a_logger_block_that_leaves_the_verify_channel_alone_is_clean(logger_block):
    assert silencers_of(logger_block, VERIFY_LOGGER) == set()


@pytest.mark.parametrize(
    ("logger_block", "expected"),
    [
        # Named exactly.
        (
            {"default": "info", "logs": {"frontend.js.modern": "critical"}},
            {"frontend.js.modern"},
        ),
        # Silenced by an ancestor prefix — the way this would actually get broken.
        ({"default": "info", "logs": {"frontend": "fatal"}}, {"frontend"}),
        ({"default": "info", "logs": {"frontend.js": "none"}}, {"frontend.js"}),
        # No entry of its own, and the block's default swallows it.
        (
            {"default": "critical", "logs": {"pychromecast.controllers": "critical"}},
            {"default"},
        ),
    ],
)
def test_a_logger_block_that_silences_the_verify_channel_is_flagged(
    logger_block, expected
):
    assert silencers_of(logger_block, VERIFY_LOGGER) == expected


def test_the_shipped_config_leaves_the_verify_channel_audible():
    block = _ha_logger_block()
    offenders = silencers_of(block, VERIFY_LOGGER)
    assert offenders == set(), (
        f"{HA_CONFIG.relative_to(REPO)} silences {VERIFY_LOGGER} via {sorted(offenders)}. "
        f"That is the only channel {PLATFORM_DOC.relative_to(REPO)} leaves for telling whether "
        "the cast connect-back fault is firing, because #2781 silenced both records it emits on "
        "pychromecast.controllers. Silence it too and the fault becomes unobservable."
    )


def test_the_guard_reads_a_real_logger_block():
    """Non-vacuity: an empty or missing `logs:` map would pass everything above."""
    block = _ha_logger_block()
    logs = block.get("logs") or {}
    assert SILENCED_BY_2781 in logs, (
        f"{SILENCED_BY_2781} is gone from {HA_CONFIG.relative_to(REPO)}'s logger block. Either "
        "this guard is reading the wrong file, or #2781's silence was dropped — in which case "
        f"the cast section of {PLATFORM_DOC.relative_to(REPO)} needs rewriting, because it "
        "explains the verify signal by reference to that silence."
    )
    assert logs[SILENCED_BY_2781] in SILENT_LEVELS


def test_the_platform_doc_names_the_channel_this_guard_protects():
    """The guard and the doc have to name the same logger, or the guard protects nothing."""
    assert VERIFY_LOGGER in PLATFORM_DOC.read_text()
