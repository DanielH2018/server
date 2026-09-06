"""The *arr Discord Connect notification must be declared, not left as UI state.

THE INJECTION POINT IS `find_notification` / `declared_body` / `needs_update`, deliberately.
Those three decide whether a deploy creates, rewrites or leaves the row alone, which is the
part with the traps in it: a match key that adopts nothing creates a SECOND notification at the
same webhook and posts every event twice, a body built from scratch blanks the field lists the
app populated, and a comparison that rewrites an identical row reports a change on every
deploy.

Every rule below is an accept/reject pair, so a check that stopped matching fails its own test
rather than passing on both halves.

Run: uv run pytest ansible/roles/k8s/arr-notification/tests
"""

import copy
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

import seed_arr_notification as seed


URL = "https://discord.com/api/webhooks/123/abc"
NAME = "Discord (health)"

# Transcribed from sonarr's live `GET /api/v3/notification`, values redacted. The trigger keys
# are sonarr's own set — radarr's differs (onMovieAdded rather than onSeriesAdd), which is why
# `trigger_keys` derives them from the object instead of listing them.
LIVE = {
    "id": 3,
    "name": NAME,
    "implementation": "Discord",
    "implementationName": "Discord",
    "configContract": "DiscordSettings",
    "infoLink": "https://wiki.servarr.com/sonarr/supported#discord",
    "tags": [],
    "includeHealthWarnings": False,
    "onGrab": False,
    "onDownload": False,
    "onUpgrade": False,
    "onImportComplete": False,
    "onRename": False,
    "onSeriesAdd": False,
    "onSeriesDelete": False,
    "onHealthIssue": True,
    "onHealthRestored": True,
    "onApplicationUpdate": False,
    "onManualInteractionRequired": False,
    "supportsOnGrab": True,
    "supportsOnHealthIssue": True,
    "fields": [
        {"order": 0, "name": "webHookUrl", "value": URL},
        {"order": 1, "name": "username", "value": "Sonarr"},
        {"order": 2, "name": "avatar"},
        {"order": 4, "name": "grabFields", "value": [0, 1, 2]},
        {"order": 5, "name": "importFields", "value": [0, 1, 2]},
    ],
}

HEALTH_ONLY = ["onHealthIssue", "onHealthRestored"]


def declare(
    existing: dict,
    triggers: list[str] | None = None,
    username: str = "Sonarr",
) -> dict:
    """`declared_body` with this role's defaults, so each test states only what it varies."""
    return seed.declared_body(
        existing,
        name=NAME,
        webhook_url=URL,
        username=username,
        triggers=HEALTH_ONLY if triggers is None else triggers,
        include_health_warnings=False,
    )


def fields_of(body: dict) -> list[dict]:
    return body["fields"]


# ── the match key ───────────────────────────────────────────────────────────────────────────


def test_the_live_discord_notification_is_found_by_name() -> None:
    assert seed.find_notification([LIVE], NAME) is LIVE


def test_a_same_named_notification_of_another_kind_is_not_adopted() -> None:
    # sonarr also carries an "Episode Trimmer" CustomScript. Matching on name alone would let a
    # renamed script be overwritten with a Discord spec, silently retiring the script hook.
    script = {"id": 2, "name": NAME, "implementation": "CustomScript", "fields": []}
    assert seed.find_notification([script], NAME) is None


def test_a_differently_named_discord_notification_is_not_adopted() -> None:
    # The reject half of the same rule: a name that matches nothing must return None so the
    # caller creates one, rather than quietly adopting whatever Discord row happens to exist.
    assert seed.find_notification([LIVE], "Discord") is None


# ── the rewrite decision ────────────────────────────────────────────────────────────────────


def test_the_live_row_is_already_what_we_declare_so_no_deploy_rewrites_it() -> None:
    # The no-op half. Both apps were configured by hand with exactly this spec, so adopting
    # them must report zero changes — a rewrite on every deploy is a permanent false `changed`.
    assert not seed.needs_update(LIVE, declare(LIVE))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b.update(onGrab=True), id="a trigger enabled by hand"),
        pytest.param(
            lambda b: b.update(onHealthIssue=False), id="a declared trigger turned off"
        ),
        pytest.param(
            lambda b: b.update(includeHealthWarnings=True), id="health warnings toggled"
        ),
        pytest.param(
            lambda b: b["fields"].__setitem__(
                0, {"order": 0, "name": "webHookUrl", "value": "https://rotated/"}
            ),
            id="a rotated webhook",
        ),
        pytest.param(
            lambda b: b["fields"].__setitem__(
                1, {"order": 1, "name": "username", "value": "sonarr-old"}
            ),
            id="a renamed poster",
        ),
    ],
)
def test_drift_from_the_declaration_is_rewritten(mutate) -> None:
    drifted = copy.deepcopy(LIVE)
    mutate(drifted)
    assert seed.needs_update(drifted, declare(drifted))


def test_the_field_lists_the_app_populated_survive_the_rewrite() -> None:
    # The update path merges into the body the API returned. Building one from scratch would
    # blank grabFields/importFields, which the app repopulates as empty — a silent content loss
    # from a task whose whole job is to change two booleans.
    body = declare(LIVE)
    by_name = {f["name"]: f for f in fields_of(body)}
    assert by_name["grabFields"]["value"] == [0, 1, 2]
    assert by_name["importFields"]["value"] == [0, 1, 2]
    assert body["configContract"] == "DiscordSettings"
    assert body["id"] == 3


# ── the trigger set is the whole truth ──────────────────────────────────────────────────────


def test_every_trigger_the_app_carries_is_declared_true_or_false() -> None:
    body = declare(LIVE, triggers=["onGrab"])
    assert body["onGrab"] is True
    # Not just "the others are unchanged" — they must be actively set false, or a trigger
    # enabled in the UI survives a deploy that is meant to declare the whole set.
    assert body["onHealthIssue"] is False
    assert body["onHealthRestored"] is False
    # `supportsOnX` is a capability flag, not a trigger, and must not be touched.
    assert body["supportsOnGrab"] is True


def test_a_trigger_name_the_app_does_not_carry_fails_the_deploy() -> None:
    # The reject half: a typo'd or radarr-only key against sonarr writes a field the app
    # ignores, leaving the notification off while the deploy reports success.
    with pytest.raises(seed.SeedError, match="onMovieAdded"):
        declare(LIVE, triggers=["onMovieAdded"])


def test_supports_flags_are_not_mistaken_for_triggers() -> None:
    assert "supportsOnGrab" not in seed.trigger_keys(LIVE)
    assert "onGrab" in seed.trigger_keys(LIVE)


# ── the create path ─────────────────────────────────────────────────────────────────────────


def test_the_schema_entry_becomes_a_postable_body() -> None:
    schema = [
        {"implementation": "Webhook", "fields": []},
        {
            "implementation": "Discord",
            "configContract": "DiscordSettings",
            "presets": [{"name": "a preset"}],
            "includeHealthWarnings": False,
            "onGrab": False,
            "onHealthIssue": False,
            "onHealthRestored": False,
            "fields": [
                {"order": 0, "name": "webHookUrl", "value": None},
                {"order": 1, "name": "username", "value": None},
                {"order": 4, "name": "grabFields", "value": [0, 1]},
            ],
        },
    ]
    base = seed.schema_for(schema)
    # `presets` is UI scaffolding the API rejects on POST.
    assert "presets" not in base
    body = declare(base)
    by_name = {f["name"]: f for f in fields_of(body)}
    assert by_name["webHookUrl"]["value"] == URL
    assert by_name["username"]["value"] == "Sonarr"
    assert by_name["grabFields"]["value"] == [0, 1]
    assert body["onHealthIssue"] is True
    assert "id" not in body


def test_a_schema_without_discord_fails_rather_than_seeding_nothing() -> None:
    with pytest.raises(seed.SeedError, match="Discord"):
        seed.schema_for([{"implementation": "Webhook", "fields": []}])


def test_a_notification_missing_the_webhook_field_fails_the_deploy() -> None:
    # The reject half of set_field. A silent skip would leave the URL unwritten and still
    # report the notification as declared.
    stripped = copy.deepcopy(LIVE)
    stripped["fields"] = [f for f in fields_of(stripped) if f["name"] != "webHookUrl"]
    with pytest.raises(seed.SeedError, match="webHookUrl"):
        declare(stripped)
