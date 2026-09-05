"""Guard: a credential-shaped `Config` field must be `field(repr=False)`.

`test_repr_hides_every_credential_but_not_ordinary_config` in `test_check_parsing.py` anchors a
sentinel list against `{f.name for f in fields(Config) if not f.repr}` by EQUALITY. That fails
when a marker is added without a sentinel and when a marker is dropped from a listed name — but
a NEW credential-bearing field added with NEITHER marker nor sentinel leaves both sides of that
equality untouched and passes. Its value then reaches `repr(cfg)`, which a `print(cfg)`, an
f-string in a log line or a traceback rendering locals puts in the pod log, and promtail ships
the pod log to Loki.

This guard closes that by shape instead of by list: any field whose NAME looks like a
credential must be hidden from the repr, whether or not anyone remembered a sentinel.

The census reads `fields(Config)` rather than grepping the four `config_*.py` modules the
issue's remedy named. `repr(cfg)` is GENERATED from `fields(Config)`, so that set is not a
proxy for the leak surface — it is the leak surface. A grep over four hardcoded module paths is
the proxy, and it would miss a credential added to a fifth domain module composed into `Config`
later. Each domain base is also constructed standalone by its builder (`config_io.io_config()`
returns `IoConfig(...)`), but an inherited field keeps the `field()` spec it was declared with,
so covering `Config` covers those reprs too.
"""

import re
from dataclasses import dataclass, field, fields

from bridge.config import Config

# Substring, not anchored: `B2_PROBE_KEY_ID` is a credential and `_KEY$` would not match it.
# A loose pattern over-matches by design — `_NOT_A_CREDENTIAL` below is where a false positive
# goes, so that the fix for one is never to weaken this.
_CREDENTIAL_SHAPE = re.compile(r"TOKEN|KEY|PASSWORD|PASSWD|SECRET|WEBHOOK|CREDENTIAL")

# Fields whose name matches the shape above but which carry nothing secret — a `LABEL_KEY`, a
# `SORT_KEY`. Empty today. Add a name here WITH A REASON rather than loosening the pattern, and
# note that the test below requires every name here to still be a field of `Config`, so a
# renamed field cannot leave a stale entry silently widening the exemption.
_NOT_A_CREDENTIAL: frozenset[str] = frozenset()

# The non-vacuity anchor. A census that finds nothing — a broken pattern, a moved module, a
# `Config` that failed to compose its bases — makes every assertion below vacuously true. These
# are the 16 credential-bearing fields PR #1147 marked; the census must still contain all of
# them, and the failure message names whichever went missing.
_KNOWN_CREDENTIAL_FIELDS = frozenset(
    {
        "B2_PROBE_KEY_ID",
        "B2_PROBE_APPLICATION_KEY",
        "CF_ANALYTICS_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_CROWDSEC_WEBHOOK_URL",
        "DISCORD_GITOPS_WEBHOOK_URL",
        "DISCORD_ARR_WEBHOOK_URL",
        "DISCORD_HEALTHCHECKS_WEBHOOK_URL",
        "SMTP_PASSWORD",
        "N8N_API_KEY",
        "SONARR_API_KEY",
        "RADARR_API_KEY",
        "BAZARR_API_KEY",
        "PROWLARR_API_KEY",
        "HA_TOKEN",
        "SPEEDTEST_TOKEN",
    }
)


def _credential_shaped(cls) -> set[str]:
    """Every field of `cls` whose name looks like a credential, exemptions removed."""
    return {
        f.name
        for f in fields(cls)
        if _CREDENTIAL_SHAPE.search(f.name) and f.name not in _NOT_A_CREDENTIAL
    }


def _unmarked_credential_fields(cls) -> set[str]:
    """The finding: credential-shaped fields of `cls` that the auto-`__repr__` would render."""
    shaped = _credential_shaped(cls)
    return {f.name for f in fields(cls) if f.name in shaped and f.repr}


def test_real_config_credential_fields_are_clean():
    """The accepting half, over the object that actually holds the secrets."""
    found = _credential_shaped(Config)
    assert found >= _KNOWN_CREDENTIAL_FIELDS, (
        "the credential census lost "
        f"{sorted(_KNOWN_CREDENTIAL_FIELDS - found)} — the pattern, the exemption list or the "
        "composition of Config changed, and this guard is now checking less than it was"
    )

    every_name = {f.name for f in fields(Config)}
    stale = sorted(_NOT_A_CREDENTIAL - every_name)
    assert not stale, (
        f"_NOT_A_CREDENTIAL names {stale}, which are no longer fields of Config — a renamed "
        "field left a stale exemption that would silently cover a future field of that name"
    )

    unmarked = sorted(_unmarked_credential_fields(Config))
    assert not unmarked, (
        f"{unmarked} look like credentials but are missing `field(repr=False)`. A print(cfg), "
        "an f-string in a log line or a traceback would ship their values to Loki. Mark each "
        "`field(repr=False)` at its declaration in the domain module, or — if the name only "
        "LOOKS like a credential — add it to _NOT_A_CREDENTIAL with a reason."
    )


def test_marked_credential_field_is_clean():
    """A credential-shaped field that IS marked passes, so the guard is not simply refusing."""

    @dataclass(frozen=True)
    class Marked:
        KUMA_URL: str = "http://uptime-kuma:3001"
        NEW_SERVICE_TOKEN: str = field(default="", repr=False)

    assert _credential_shaped(Marked) == {"NEW_SERVICE_TOKEN"}
    assert _unmarked_credential_fields(Marked) == set()


def test_unmarked_credential_field_is_flagged():
    """The rejecting half: exactly the case the equality-anchored sentinel test cannot see.

    A new credential-bearing field, added with neither `repr=False` nor a sentinel in
    `test_check_parsing.py`. That test still passes on this class; this one must not.
    """

    @dataclass(frozen=True)
    class Unmarked:
        KUMA_URL: str = "http://uptime-kuma:3001"
        NEW_SERVICE_TOKEN: str = ""
        NEW_DISCORD_WEBHOOK_URL: str = ""

    assert _unmarked_credential_fields(Unmarked) == {
        "NEW_SERVICE_TOKEN",
        "NEW_DISCORD_WEBHOOK_URL",
    }
