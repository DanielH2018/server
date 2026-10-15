"""What `shadow_diff.py stubs` writes: the extra-vars file both halves of the shadow render read.

Run: uv run pytest scripts/renderer/tests/test_renderer_stub_file.py
"""

from pathlib import Path

import pytest
import yaml

from lib import yaml_fast
from renderer import shadow_diff
from secret_stubs import homelab_secret_stub, homelab_stub_keys


def test_stubs_writes_the_same_file_twice_for_one_tree(tmp_path):
    first, second = tmp_path / "a.yml", tmp_path / "b.yml"
    assert shadow_diff.main(["stubs", "--out", str(first)]) == 0
    assert shadow_diff.main(["stubs", "--out", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()


def unexpected_entries(stub_file: Path) -> dict[str, object]:
    """Each entry of `stub_file` that is not R1's stub of a secret key or the sentinel.

    R3's exit check 1: the renderer pins no live fact, so the file holds exactly R1's
    secret-key stub map plus `homelab_secret_stubs: true`. A missing key comes back with the
    value `<missing>`.
    """
    loaded = yaml_fast.safe_load(stub_file.read_text())
    keys = homelab_stub_keys(yaml_fast.safe_load(shadow_diff.SECRETS.read_text()))
    expected: dict[str, object] = {key: homelab_secret_stub(key) for key in keys}
    expected[shadow_diff.SENTINEL] = True
    extra = {k: v for k, v in loaded.items() if expected.get(k, object()) != v}
    return extra | {k: "<missing>" for k in expected.keys() - loaded.keys()}


def test_the_stub_file_holds_only_the_secret_stubs_and_the_sentinel_is_clean(tmp_path):
    out = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(out)]) == 0
    assert unexpected_entries(out) == {}
    # Non-vacuity: the file holds a secret key's stub, not only the sentinel.
    assert yaml_fast.safe_load(out.read_text())["email"] == "stub-email"


def test_a_stub_writer_that_adds_a_non_secret_key_is_flagged(tmp_path):
    # The fixture writer is the test itself: it dumps the real stub map plus one live fact.
    written = {**shadow_diff.stub_vars(), "widget_live_fact": "x"}
    out = tmp_path / "stubs.yml"
    out.write_text(yaml.safe_dump(written, sort_keys=True))
    assert unexpected_entries(out) == {"widget_live_fact": "x"}


def test_stub_vars_over_a_secrets_file_without_clashes_is_clean(tmp_path):
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("email: ENC[x]\nsops:\n  version: 3\n")
    assert shadow_diff.stub_vars(secrets)["email"] == "stub-email"


def test_stub_vars_over_a_secret_key_named_like_the_sentinel_is_flagged(tmp_path):
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("email: ENC[x]\nhomelab_secret_stubs: ENC[x]\n")
    with pytest.raises(ValueError, match="homelab_secret_stubs"):
        shadow_diff.stub_vars(secrets)
