"""The Alloy `stage.replace` must redact HA's cast `refresh_token` and nothing else.

Home Assistant writes the Google Cast system user's `refresh_token` in plaintext whenever a cast
fails in `_handle_signal_show_view`, and `homeassistant.util.logging` is not silenced, so the
record reaches Loki. The stage that strips it has two ways to be wrong, and the
pair below is one test each:

- it stops matching, and a live HA API credential sits in a 744h log store;
- it eats the whole record, and `|= "_handle_signal_show_view"` — the one query that still
  catches a cast timed out on a worker thread (`docs/platform.md`) — returns nothing.

The expression is READ OUT of the rendered config rather than restated here, so the assertions
cannot drift from what ships. Alloy compiles it as RE2; Python's `re` stands in, which is
equivalent for this pattern (a literal, a character class, a bounded repeat, one capture group).
"""

import re

import pytest

from _k8s_render import rendered_docs

# The record's shape. HA formats the `cast_show_view` signal args into the message.
# The stand-in keeps the real token's length (64 hex chars) and carries no entropy, so gitleaks
# does not read the fixture as a credential.
TOKEN = "f" * 64
RECORD = (
    "Exception in _handle_signal_show_view when dispatching 'cast_show_view': "
    "({'hass_url': 'https://home-assistant.example', 'hass_uuid': 'abc', 'client_id': None, "
    f"'refresh_token': '{TOKEN}'}}, ChromecastInfo(...), 'lovelace', 0)"
)

# `stage.replace { expression = "…" ; replace = "…" }`, inside the home-assistant match block.
_REPLACE_STAGE = re.compile(
    r"stage\.replace\s*\{\s*expression\s*=\s*\"(.+?)\"\s*\n\s*replace\s*=\s*\"(.+?)\"",
)


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


@pytest.fixture(scope="module")
def redact(alloy_config: str):
    """The shipped expression/replacement as a callable, decoded out of the River string."""
    m = _REPLACE_STAGE.search(alloy_config)
    assert m, "no stage.replace in the rendered Alloy config"
    # River string literals use Go escapes; `\"` is the only one this expression needs.
    expression = m.group(1).replace('\\"', '"')
    replacement = m.group(2)
    pattern = re.compile(expression)
    assert pattern.groups == 1, (
        f"stage.replace substitutes only the CAPTURE GROUP, so {expression!r} must have exactly "
        "one; with none, Alloy replaces the whole match and the record loses its marker"
    )
    return lambda line: pattern.sub(replacement, line)


def test_the_token_is_redacted(redact) -> None:
    assert TOKEN not in redact(RECORD)
    assert "<redacted>" in redact(RECORD)


def test_the_detection_marker_is_printed(redact) -> None:
    """The record survives apart from the token — this is what keeps the fault findable."""
    out = redact(RECORD)
    assert "_handle_signal_show_view" in out
    assert "cast_show_view" in out
    assert "hass_url" in out


def test_a_null_refresh_token_is_printed(redact) -> None:
    """Nothing to hide, and a stage that rewrote it would hide a real diagnostic."""
    line = "cast failed: {'client_id': None, 'refresh_token': None}"
    assert redact(line) == line


def test_the_stage_is_scoped_to_home_assistant(alloy_config: str) -> None:
    """Every container's lines pass through loki.process, so the match block is the scope."""
    # `.index` would land in the block comment above the stage, which names it in prose.
    before = alloy_config[: re.search(r"stage\.replace\s*\{", alloy_config).start()]
    enclosing_selector = before[before.rindex("selector =") :].splitlines()[0]
    assert 'container=\\"home-assistant\\"' in enclosing_selector, enclosing_selector
