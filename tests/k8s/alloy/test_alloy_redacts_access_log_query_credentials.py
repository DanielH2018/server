"""The Alloy config must redact credential-bearing query parameters out of Traefik's access log.

Traefik's JSON access log carries the full request line in `RequestPath`, query string included,
and the `access-log-rotate` sidecar copies it to stdout, where Alloy ships it to `loki-homelab`.
The redaction has two ways to be wrong, and each has a test below:

- a credential survives, and anyone who can read the 744h store can read it;
- a parameter that is not a credential goes with it (`p=` is pagination outside `/rest/`), or the
  whole record does, and a count by parameter name stops working.

The stages are READ OUT of the rendered config by `_alloy_pods_pipeline`. `probe.py` redacts the
records already in Loki with the same names (`redact_log_secrets`), so the last two tests hold the
two implementations equal: a name added to one and not the other fails here.
"""

import re

import pytest

from _alloy_pods_pipeline import expressions_for, ship
from diagnostics.probe_lib import core

SCOPED_CONTAINER = "access-log-rotate"

# Obviously fake values: nothing here is entropy for gitleaks to read as a credential.
FAKE = "notasecret1"

# Names the redaction must cover, in the case an application writes them.
GENERIC_NAMES = (
    "api_key",
    "ApiKey",
    "apikey",
    "APIKEY",
    "access_token",
    "token",
    "code",
)


def _access_line(request_path: str) -> str:
    """A Traefik JSON access line, fields in the order Traefik emits them (alphabetical)."""
    return (
        '{"ClientHost":"10.42.1.92","DownstreamStatus":200,"Duration":1200000000,'
        f'"RequestHost":"app.local.example.com","RequestMethod":"GET","RequestPath":"{request_path}",'
        '"RequestProtocol":"HTTP/2.0","StartUTC":"2026-11-12T10:00:00Z"}'
    )


def _ship(line: str) -> str:
    return ship(SCOPED_CONTAINER, line)


@pytest.mark.parametrize("name", GENERIC_NAMES)
def test_a_credential_parameter_value_is_redacted(name: str) -> None:
    out = _ship(_access_line(f"/api/items?{name}={FAKE}"))
    assert FAKE not in out
    assert f"{name}=<redacted>" in out


def test_only_the_value_is_redacted() -> None:
    """The name stays countable and the rest of the record survives."""
    out = _ship(_access_line(f"/cb?access_token={FAKE}&x=1"))
    assert '"RequestPath":"/cb?access_token=<redacted>&x=1"' in out
    assert '"DownstreamStatus":200,' in out
    assert '"RequestMethod":"GET"' in out


def test_every_credential_on_one_path_is_redacted() -> None:
    out = _ship(_access_line(f"/cb?code={FAKE}&state=s1&token={FAKE}2"))
    assert FAKE not in out
    assert "?code=<redacted>&state=s1&token=<redacted>" in out


def test_a_json_escaped_ampersand_ends_the_value() -> None:
    out = _ship(_access_line(f"/cb?x=1\\u0026code={FAKE}\\u0026y=2"))
    assert out.count("<redacted>") == 1
    assert "\\u0026code=<redacted>\\u0026y=2" in out


def test_a_webhook_path_loses_both_its_id_and_its_token() -> None:
    """The webhook-id stage and these stages both apply to one line, and neither shadows the other."""
    webhook_id = "f" * 64
    out = _ship(_access_line(f"/api/webhook/{webhook_id}?token={FAKE}"))
    assert webhook_id not in out
    assert FAKE not in out
    assert "/api/webhook/<redacted>?token=<redacted>" in out


@pytest.mark.parametrize(
    "request_path",
    [
        "/api/items",
        "/api/items?page=2&sortKey=name",
        "/api/items?encode=1&barcode=2&tokens=3",
        "/api/items?refresh_token_hint=1",
        "/api/items?api_key=",
        "/search?q=api_key=1",
    ],
)
def test_a_path_with_no_credential_parameter_is_unchanged(request_path: str) -> None:
    line = _access_line(request_path)
    assert _ship(line) == line


def test_the_stage_is_scoped_to_the_access_log_sidecar() -> None:
    line = _access_line(f"/cb?code={FAKE}")
    assert ship("home-assistant", line) == line


@pytest.mark.parametrize("name", core.SUBSONIC_REST_PARAMS)
def test_a_subsonic_parameter_under_rest_is_redacted(name: str) -> None:
    out = _ship(_access_line(f"/rest/ping?v=1.16&{name}={FAKE}&c=app"))
    assert FAKE not in out
    assert f"&{name}=<redacted>&c=app" in out


def test_every_subsonic_parameter_on_one_path_is_redacted() -> None:
    out = _ship(_access_line("/rest/ping?u=alice&p=pw&t=tok&s=salt&c=app"))
    assert "/rest/ping?u=<redacted>&p=<redacted>&t=<redacted>&s=<redacted>&c=app" in out


def test_a_json_escaped_ampersand_before_a_subsonic_parameter() -> None:
    out = _ship(_access_line("/rest/ping?u=alice\\u0026p=pw\\u0026c=app"))
    assert "/rest/ping?u=<redacted>\\u0026p=<redacted>\\u0026c=app" in out


@pytest.mark.parametrize(
    "request_path",
    [
        "/api/items?p=2",
        "/api/rest/items?p=2&s=10",
        "/restful?u=1",
        "/x?u=1&t=2&s=3&p=4",
    ],
)
def test_a_subsonic_name_outside_rest_is_unchanged(request_path: str) -> None:
    line = _access_line(request_path)
    assert _ship(line) == line


def _redacted_names(redact, path_prefix: str, names) -> set[str]:
    """The names, among `names`, that `redact` blanks on a URL under `path_prefix`."""
    return {
        name for name in names if FAKE not in redact(f"{path_prefix}?{name}={FAKE}")
    }


def test_alloy_and_probe_redact_the_same_subsonic_names() -> None:
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    alloy = _redacted_names(
        lambda path: _ship(_access_line(path)), "/rest/ping", letters
    )
    probe = _redacted_names(core.redact_log_secrets, "/rest/ping", letters)
    assert alloy == probe == set(core.SUBSONIC_REST_PARAMS)


def test_probe_redacts_every_generic_name_alloy_does() -> None:
    expression = next(e for e in expressions_for(SCOPED_CONTAINER) if "api_key" in e)
    alternation = re.search(r"\(\?:((?:\w+\|)+\w+)\)=", expression)
    assert alternation, f"no name alternation in {expression!r}"
    names = alternation.group(1).split("|")
    assert set(names) >= {n.lower() for n in GENERIC_NAMES}
    for name in names:
        out = core.redact_log_secrets(f"/api/items?{name}={FAKE}")
        assert FAKE not in out, f"probe prints a {name}= value Alloy redacts"
