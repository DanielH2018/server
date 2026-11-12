"""The Alloy config must redact Prowlarr's `apikey=` out of the *arr Torznab error lines (#6276).

Sonarr and Radarr log every failed Torznab call with its full request URL, and that URL embeds
Prowlarr's API key as `apikey=<32 hex>`. Alloy ships those lines to `loki-homelab`, which the LAN
reaches with no Authelia in front of it. The redaction has two ways to be wrong, and each has a
test below:

- the key survives, and anyone on the LAN can read Prowlarr's key from the 744h store;
- the whole record goes with it, and the indexer name and search type a Torznab failure is
  diagnosed by are lost.

The stages are READ OUT of the rendered config by `_alloy_pods_pipeline.ship` rather than
restated here.
"""

import re

import pytest

from _alloy_pods_pipeline import ship

# The real key's shape (32 hex chars), with no entropy for gitleaks to read as a credential.
KEY = "f" * 32


def _torznab_line(app: str, key: str = KEY) -> str:
    search = "tvsearch" if app == "sonarr" else "movie"
    return (
        "[Warn] Torznab: Tokyo Toshokan (Prowlarr) server is currently unavailable. "
        f"http://prowlarr:9696/21/api?t={search}&cat=5000&extended=1&apikey={key}"
        "&offset=0&limit=100"
    )


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_the_torznab_apikey_is_redacted(app: str) -> None:
    """The Verify-by: no `apikey=[0-9a-f]{32}` line reaches Loki after a Torznab failure."""
    out = ship(app, _torznab_line(app))
    assert KEY not in out
    assert re.search(r"apikey=[0-9a-f]{32}", out) is None
    assert "apikey=<redacted>" in out


@pytest.mark.parametrize("app", ["prowlarr", "bazarr"])
def test_the_apikey_is_redacted_for_prowlarr_and_bazarr(app: str) -> None:
    out = ship(app, _torznab_line(app))
    assert KEY not in out


def test_an_upper_case_apikey_is_redacted() -> None:
    key = "F" * 32
    out = ship("sonarr", _torznab_line("sonarr", key))
    assert key not in out


def test_the_rest_of_the_torznab_record_is_kept() -> None:
    """Only the capture group goes: the indexer and the search type stay findable."""
    out = ship("sonarr", _torznab_line("sonarr"))
    assert KEY not in out
    assert "Tokyo Toshokan (Prowlarr)" in out
    assert "t=tvsearch" in out
    assert "&offset=0&limit=100" in out
