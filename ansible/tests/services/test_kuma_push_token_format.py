"""The uptime-kuma deploy refuses a push token AutoKuma would refuse (#3985).

AutoKuma rejects a `push_token` that is not 32 letters and digits with one WARN per sync and
never creates the monitor, so its pusher gets HTTP 404 and nothing pages. These guards evaluate
the role's own assert expression, through the `kuma_malformed_push_tokens` filter, over the
declarations the real template renders.
"""

import copy
import secrets

import pytest

from _helpers import ANSIBLE, jinja_env, load_tasks, task_named
from _kuma_entities import _entities
from kuma_monitors import FilterModule, kuma_malformed_push_tokens

_TASK = task_named(
    load_tasks(ANSIBLE / "roles/k8s/uptime-kuma/tasks/main.yml"),
    "push token is in the format AutoKuma accepts",
)

# The tile whose hand-minted token was refused on 2026-10-09. It is gated on its token, so it
# drops out of the render if the seed stops opening that gate.
_INCIDENT_TILE = "homelab-eval-sweep"


def _declarations(token_for) -> dict[str, dict]:
    """The rendered declarations keyed the way the task keys them, with every push token set.

    `token_for` maps an AutoKuma id to the token its push tile carries.
    """
    declarations = {
        name.removesuffix(".json"): copy.deepcopy(entity)
        for name, entity in _entities().items()
    }
    for ident, entity in declarations.items():
        if entity["type"] == "push":
            entity["push_token"] = token_for(ident)
    return declarations


def _assert_passes(declarations: dict[str, dict]) -> bool:
    env = jinja_env()
    env.filters.update(FilterModule().filters())
    that = _TASK["ansible.builtin.assert"]["that"]
    passed = env.from_string("{{ " + that + " }}").render(
        kuma_declarations=declarations
    )
    assert isinstance(passed, bool), f"the assert rendered {passed!r}, not a bool"
    return passed


def test_hex_tokens_on_every_push_tile_are_clean():
    assert _assert_passes(_declarations(lambda _ident: secrets.token_hex(16)))


def test_one_base64_token_is_flagged():
    # fact: ansible/roles/k8s/uptime-kuma/CLAUDE.md#Traps
    # The shape a base64 mint produces: 44 characters, padded with `=`.
    base64_token = "QUJD" * 10 + "QQ+="

    def token_for(ident: str) -> str:
        return base64_token if ident == _INCIDENT_TILE else secrets.token_hex(16)

    declarations = _declarations(token_for)
    assert not _assert_passes(declarations)
    assert kuma_malformed_push_tokens(declarations) == [_INCIDENT_TILE]


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("a" * 31, id="31-chars"),
        pytest.param("a" * 33, id="33-chars"),
        pytest.param("0123456789abcdef-0123456789abcde", id="hyphen"),
        pytest.param("", id="empty"),
        pytest.param(None, id="missing"),
    ],
)
def test_a_token_autokuma_refuses_is_flagged(token):
    entity = {"type": "push", "name": "x"}
    if token is not None:
        entity["push_token"] = token
    assert kuma_malformed_push_tokens({"tile": entity}) == ["tile"]


def test_a_monitor_that_is_not_push_is_never_flagged():
    assert (
        kuma_malformed_push_tokens({"tile": {"type": "http", "url": "https://x"}}) == []
    )


def test_the_check_covers_every_rendered_push_tile():
    # Under the test render every token is `stub-<name>`, which AutoKuma would refuse, so the
    # filter must name every push tile. A filter that stopped reading the census would pass a
    # deploy while covering nothing.
    declarations = {
        name.removesuffix(".json"): entity for name, entity in _entities().items()
    }
    push_ids = sorted(i for i, e in declarations.items() if e["type"] == "push")
    assert _INCIDENT_TILE in push_ids
    assert len(push_ids) >= 40, f"the push-tile census went thin: {len(push_ids)}"
    assert kuma_malformed_push_tokens(declarations) == push_ids
