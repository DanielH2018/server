"""The Alloy `stage.replace` must redact HA's cast `refresh_token` and nothing else.

Home Assistant writes the Google Cast system user's `refresh_token` in plaintext whenever a cast
fails in `_handle_signal_show_view`, and `homeassistant.util.logging` is not silenced, so the
record reaches Loki. The stage that strips it has two ways to be wrong, and the
pair below is one test each:

- it stops matching, and a live HA API credential sits in a 744h log store;
- it eats the whole record, and `|= "_handle_signal_show_view"` — the one query that still
  catches a cast timed out on a worker thread (`docs/platform.md`) — returns nothing.

The expression is READ OUT of the rendered config by `_alloy_pods_pipeline.ship` rather than
restated here, so the assertions cannot drift from what ships.
"""

from _alloy_pods_pipeline import ship

# The record's shape. HA formats the `cast_show_view` signal args into the message.
# The stand-in keeps the real token's length (64 hex chars) and carries no entropy, so gitleaks
# does not read the fixture as a credential.
TOKEN = "f" * 64
RECORD = (
    "Exception in _handle_signal_show_view when dispatching 'cast_show_view': "
    "({'hass_url': 'https://home-assistant.example', 'hass_uuid': 'abc', 'client_id': None, "
    f"'refresh_token': '{TOKEN}'}}, ChromecastInfo(...), 'lovelace', 0)"
)


def test_the_token_is_redacted() -> None:
    out = ship("home-assistant", RECORD)
    assert TOKEN not in out
    assert "<redacted>" in out


def test_the_detection_marker_is_printed() -> None:
    """The record survives apart from the token — this is what keeps the fault findable."""
    out = ship("home-assistant", RECORD)
    assert "_handle_signal_show_view" in out
    assert "cast_show_view" in out
    assert "hass_url" in out


def test_a_null_refresh_token_is_printed() -> None:
    """Nothing to hide, and a stage that rewrote it would hide a real diagnostic."""
    line = "cast failed: {'client_id': None, 'refresh_token': None}"
    assert ship("home-assistant", line) == line


def test_the_stage_is_scoped_to_home_assistant() -> None:
    """Every container's lines pass through loki.process, so the match block is the scope."""
    assert ship("homepage", RECORD) == RECORD
