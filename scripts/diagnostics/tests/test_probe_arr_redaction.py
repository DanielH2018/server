"""`probe.py arr`: credential-bearing field values are redacted before they are printed.

The subcommand is read-only against the *arr app, which says nothing about what it does to
the transcript. A Discord Connect notification's `webHookUrl` is a live credential and the
*arr API labels it `privacy: normal`, so the API's own label cannot carry this on its own
(issue #1388).

Every rule here is a `..._is_redacted` / `..._is_printed` pair: a rule that silently stopped
matching, and one that started matching everything, both fail their own test.
"""

import json

import pytest

from diagnostics.probe_lib import arr

# Shaped like a real Sonarr `GET /api/v3/notification` object, with the values replaced.
NOTIFICATION = [
    {
        "id": 1,
        "name": "Discord",
        "implementation": "Discord",
        "fields": [
            {
                "order": 0,
                "name": "webHookUrl",
                "label": "Webhook URL",
                "value": "https://discord.com/api/webhooks/123/SECRET-TOKEN",
                "privacy": "normal",
            },
            {
                "order": 1,
                "name": "username",
                "label": "Username",
                "value": "sonarr",
                "privacy": "normal",
            },
        ],
    }
]

DOWNLOADCLIENT = [
    {
        "id": 1,
        "name": "qBittorrent",
        "fields": [
            {"name": "host", "value": "qbittorrent", "privacy": "normal"},
            {"name": "port", "value": 8080, "privacy": "normal"},
            {"name": "username", "value": "admin", "privacy": "userName"},
            {"name": "password", "value": "hunter2", "privacy": "password"},
        ],
    }
]

INDEXER = [
    {
        "id": 3,
        "name": "Some Indexer",
        "fields": [
            {"name": "baseUrl", "value": "https://indexer.test", "privacy": "normal"},
            {"name": "apiKey", "value": "INDEXER-API-KEY", "privacy": "apiKey"},
        ],
    }
]

IMPORTLIST = [
    {
        "id": 2,
        "name": "Trakt list",
        "fields": [
            {"name": "listName", "value": "watchlist", "privacy": "normal"},
            {"name": "authUser", "value": "daniel", "privacy": "normal"},
            {"name": "accessToken", "value": "TRAKT-ACCESS-TOKEN", "privacy": "normal"},
        ],
    }
]


def _field(payload, name):
    return next(f for f in payload[0]["fields"] if f["name"] == name)


# --- one field it must redact -----------------------------------------------------------


def test_discord_webhook_url_is_redacted():
    # The reason the API's `privacy` label is not enough on its own: this one says `normal`.
    out = arr.redact_arr_payload(NOTIFICATION)
    assert _field(out, "webHookUrl")["value"] == arr.REDACTED


@pytest.mark.parametrize(
    ("payload", "field_name"),
    [
        (DOWNLOADCLIENT, "password"),
        (INDEXER, "apiKey"),
        (IMPORTLIST, "accessToken"),
    ],
)
def test_credential_field_is_redacted(payload, field_name):
    assert _field(arr.redact_arr_payload(payload), field_name)["value"] == arr.REDACTED


def test_plain_dict_key_is_redacted():
    # Not every *arr object wraps its credential in a `fields[]` entry.
    out = arr.redact_arr_payload({"name": "prowlarr", "apiKey": "TOP-SECRET"})
    assert out["apiKey"] == arr.REDACTED


# --- one field it must print ------------------------------------------------------------


def test_non_credential_field_is_printed():
    out = arr.redact_arr_payload(NOTIFICATION)
    assert _field(out, "username")["value"] == "sonarr"


@pytest.mark.parametrize(
    ("payload", "field_name", "expected"),
    [
        (DOWNLOADCLIENT, "host", "qbittorrent"),
        (DOWNLOADCLIENT, "port", 8080),
        (INDEXER, "baseUrl", "https://indexer.test"),
        (IMPORTLIST, "listName", "watchlist"),
    ],
)
def test_ordinary_field_is_printed(payload, field_name, expected):
    assert _field(arr.redact_arr_payload(payload), field_name)["value"] == expected


def test_structure_is_preserved():
    out = arr.redact_arr_payload(NOTIFICATION)
    assert out[0]["id"] == 1
    assert out[0]["implementation"] == "Discord"
    assert [f["name"] for f in out[0]["fields"]] == ["webHookUrl", "username"]


# --- the wiring: what reaches stdout ----------------------------------------------------


# `format_arr_response` is the seam run_arr prints through, so these assert the printed text
# without stubbing the kubectl / SOPS / curl boundary run_arr crosses to fetch the body.

BODY = json.dumps(NOTIFICATION)


def test_printed_output_is_redacted_by_default():
    text, rc = arr.format_arr_response(BODY)
    assert rc == 0
    assert "SECRET-TOKEN" not in text
    assert arr.REDACTED in text


def test_printed_json_output_is_redacted_by_default():
    text, rc = arr.format_arr_response(BODY, as_json=True)
    assert rc == 0
    assert "SECRET-TOKEN" not in text
    assert json.loads(text) == arr.redact_arr_payload(NOTIFICATION)


def test_show_secrets_prints_the_value():
    text, rc = arr.format_arr_response(BODY, show_secrets=True)
    assert rc == 0
    assert "SECRET-TOKEN" in text


def test_non_json_body_is_passed_through():
    text, rc = arr.format_arr_response("<html>404</html>\n")
    assert rc == 1
    assert text.strip() == "<html>404</html>"
