"""HA's `logger.filters` must drop the failed-cast record that carries the cast refresh_token.

A failed cast logs `Exception in _handle_signal_show_view when dispatching 'cast_show_view'`
on `homeassistant.util.logging`, and the dispatched args include the cast system user's
`refresh_token` in plaintext. The filter keeps that record out of stdout, where `kubectl logs`
reads it unredacted. HA attaches each pattern to the named logger and drops a record when
`re.search(pattern, record.getMessage())` matches (`homeassistant/components/logger`), so a
typo in the logger name attaches the filter to a logger nothing writes to and every other check
stays green. These tests apply that same rule, on that logger name, to the shipped config.
"""

import re
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "files" / "configuration.yaml"

DISPATCH_LOGGER = "homeassistant.util.logging"

TOKEN = "f" * 64
FAILED_CAST = (
    "Exception in _handle_signal_show_view when dispatching 'cast_show_view': "
    "({'hass_url': 'https://home-assistant.example.com', 'hass_uuid': 'abc', "
    f"'client_id': None, 'refresh_token': '{TOKEN}'}}, ChromecastInfo(...), 'lovelace', 0)"
)
OTHER_DISPATCH_ERROR = (
    "Exception in _async_update when dispatching 'zha_update': (None,)"
)


def _filters() -> dict[str, list[str]]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    config = yaml.load(CONFIG.read_text(), Loader=_Loader)
    return config["logger"]["filters"]


def _dropped(message: str) -> bool:
    patterns = [re.compile(p) for p in _filters().get(DISPATCH_LOGGER, [])]
    return any(p.search(message) for p in patterns)


def test_a_failed_cast_record_is_flagged():
    assert _dropped(FAILED_CAST)


def test_a_dispatch_error_without_a_token_is_clean():
    assert not _dropped(OTHER_DISPATCH_ERROR)
