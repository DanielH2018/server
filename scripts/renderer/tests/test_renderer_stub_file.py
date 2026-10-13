"""What `shadow_diff.py stubs` writes: the extra-vars file both halves of the shadow render read.

Run: uv run pytest scripts/renderer/tests/test_renderer_stub_file.py
"""

import pytest

from lib import yaml_fast
from renderer import shadow_diff
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS
from secret_stubs import homelab_secret_stub, homelab_stub_keys

GA = "google_assistant_service_account"


def test_stubs_writes_the_same_file_twice_for_one_tree(tmp_path):
    first, second = tmp_path / "a.yml", tmp_path / "b.yml"
    assert shadow_diff.main(["stubs", "--out", str(first)]) == 0
    assert shadow_diff.main(["stubs", "--out", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()


def test_stub_file_holds_every_stub_the_sentinel_and_the_pinned_facts(tmp_path):
    out = tmp_path / "stubs.yml"
    shadow_diff.main(["stubs", "--out", str(out)])
    loaded = yaml_fast.safe_load(out.read_text())

    keys = homelab_stub_keys(yaml_fast.safe_load(shadow_diff.SECRETS.read_text()))
    assert {key: loaded[key] for key in keys} == {
        key: homelab_secret_stub(key) for key in keys
    }
    assert loaded["homelab_secret_stubs"] is True
    assert {name: loaded[name] for name in PINNED_LIVE_FACTS} == {
        name: fact.value for name, fact in PINNED_LIVE_FACTS.items()
    }
    assert set(loaded) == {*keys, "homelab_secret_stubs", *PINNED_LIVE_FACTS}
    # A JSON object is also a YAML flow mapping; the file must still load it as a string.
    assert isinstance(loaded[GA], str)
    assert loaded[GA] == homelab_secret_stub(GA)


def test_stub_vars_over_a_secrets_file_without_clashes_is_clean(tmp_path):
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("domain: ENC[x]\nsops:\n  version: 3\n")
    assert shadow_diff.stub_vars(secrets)["domain"] == "stub-domain"


def test_stub_vars_over_a_secret_key_named_like_a_pinned_fact_is_flagged(tmp_path):
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("domain: ENC[x]\nauthelia_password_hash: ENC[x]\n")
    with pytest.raises(ValueError, match="authelia_password_hash"):
        shadow_diff.stub_vars(secrets)
