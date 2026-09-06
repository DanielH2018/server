"""The Discord channel healthchecks alerts through must be declared, not left as UI state.

THE INJECTION POINT IS `desired_spec` / `needs_update`, deliberately. Those decide what the
row holds and whether a deploy rewrites it, which is the part with the traps in it: a spec
missing its `up` half raises inside `sendalerts` on the recovery flip, and a comparison that
reads the stored value as text rewrites a matching row on every deploy.

Every rule below is an accept/reject pair, so a check that stopped matching fails its own
test rather than passing on both halves.

Run: uv run pytest ansible/roles/k8s/healthchecks/tests
"""

import json
import os
import sys

from _k8s_render import rendered_docs

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

import seed_discord_channel as seed


URL = "https://discord.com/api/webhooks/123/abc"


def test_both_halves_of_the_spec_resolve_the_way_the_transport_reads_them() -> None:
    # Channel.webhook_spec(status) reads method_/url_/body_/headers_ for the status it is
    # given. sendalerts asks for "down" on the failure and "up" on the recovery, so a spec
    # carrying only the down keys raises the first time a check comes back.
    spec = seed.desired_spec(URL)
    for status in ("down", "up"):
        for field in ("method", "url", "body", "headers"):
            assert spec[f"{field}_{status}"], f"{field}_{status} is missing or empty"
    assert spec["url_down"] == URL
    assert spec["url_up"] == URL
    # The body has to be the JSON Discord reads a message out of, not a bare string.
    assert spec["body_down"] == seed.BODY_DOWN
    assert spec["body_up"] == seed.BODY_UP
    assert json.loads(seed.BODY_DOWN)["content"]
    assert json.loads(seed.BODY_UP)["content"]


def test_needs_update_is_clean_when_the_stored_spec_already_matches() -> None:
    assert seed.needs_update(seed.desired_value(URL), URL) is False


def test_needs_update_is_clean_when_the_stored_keys_are_in_another_order() -> None:
    # healthchecks' own form writes these keys in its order, not ours. Comparing text rather
    # than documents would report a change on every deploy and rewrite a matching row.
    shuffled = json.dumps(dict(reversed(list(seed.desired_spec(URL).items()))))
    assert seed.needs_update(shuffled, URL) is False


def test_needs_update_is_flagged_when_the_stored_url_differs() -> None:
    stale = seed.desired_spec("https://discord.com/api/webhooks/999/rotated-away")
    assert seed.needs_update(json.dumps(stale), URL) is True


def test_needs_update_is_flagged_when_the_stored_spec_has_only_its_down_half() -> None:
    half = {k: v for k, v in seed.desired_spec(URL).items() if not k.endswith("_up")}
    assert seed.needs_update(json.dumps(half), URL) is True


def test_stored_spec_rejects_every_unusable_shape() -> None:
    # Each is a row a half-finished UI edit can leave behind. All mean the same thing to the
    # caller: rewrite it.
    for value in ("", "not json", "[]", '"a string"', "17"):
        assert seed.stored_spec(value) is None, value


def test_the_secret_carries_the_webhook_env_the_seed_reads() -> None:
    # Without this key the seed raises SystemExit inside the pod rather than silently writing
    # nothing — but the deploy fails, so the wiring is worth pinning here too.
    secrets = [
        doc
        for role, tpl, doc in rendered_docs()
        if role == "healthchecks" and doc.get("kind") == "Secret"
    ]
    assert secrets, "the healthchecks role rendered no Secret"
    assert any(seed.WEBHOOK_ENV in doc.get("stringData", {}) for doc in secrets)
