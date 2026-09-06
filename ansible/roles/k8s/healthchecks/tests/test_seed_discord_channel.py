"""The Discord channel healthchecks alerts through must be declared, not clicked.

THE INJECTION POINT IS `stored_url` / `needs_update`, deliberately. Those decide whether a
deploy rewrites the Channel row, which is the part with the trap in it: a channel that
"exists" while holding a stale or malformed value delivers nothing and reads fine in the
Integrations list. Driving Django instead would prove only that the ORM works.

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


def test_desired_value_is_read_back_through_the_transports_own_key_path() -> None:
    # healthchecks' Discord transport reads json["webhook"]["url"]. If this shape drifts,
    # sendalerts raises a KeyError per flip and every alert is dropped.
    assert json.loads(seed.desired_value(URL))["webhook"]["url"] == URL


def test_needs_update_is_clean_when_the_stored_url_already_matches() -> None:
    assert seed.needs_update(seed.desired_value(URL), URL) is False


def test_needs_update_is_flagged_when_the_stored_url_differs() -> None:
    stale = seed.desired_value("https://discord.com/api/webhooks/999/rotated-away")
    assert seed.needs_update(stale, URL) is True


def test_stored_url_reads_a_well_formed_value() -> None:
    assert seed.stored_url(seed.desired_value(URL)) == URL


def test_stored_url_rejects_every_unusable_shape() -> None:
    # Each of these is a row a half-finished UI edit or an older healthchecks can leave
    # behind. All mean the same thing to the caller: rewrite it.
    for value in ("", "not json", "[]", '{"webhook": "a-string"}', '{"webhook": {}}'):
        assert seed.stored_url(value) is None, value


def test_the_secret_carries_the_webhook_env_the_seed_reads() -> None:
    # Without this key the seed raises SystemExit inside the pod rather than silently
    # writing nothing — but the deploy fails, so the wiring is worth pinning here too.
    secrets = [
        doc
        for role, tpl, doc in rendered_docs()
        if role == "healthchecks" and doc.get("kind") == "Secret"
    ]
    assert secrets, "the healthchecks role rendered no Secret"
    assert any(seed.WEBHOOK_ENV in doc.get("stringData", {}) for doc in secrets)
