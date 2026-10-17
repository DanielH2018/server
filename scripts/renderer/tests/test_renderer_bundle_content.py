"""The `homelab/content-digest` annotation on Kustomization `<tag>`, C7's flux reference (#5338).

The fixtures are `test_renderer_bundle.py`'s, so no role renders.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle_content.py
"""

import json

from lib import yaml_fast
from renderer.renderer_lib.bundle import CONTENT_DIGEST, DIGESTS
from test_renderer_bundle import _bundle, _pihole, _tag


def _content(files: dict[str, str], tag: str) -> str | None:
    doc = next(yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"]))
    return (doc["metadata"].get("annotations") or {}).get(CONTENT_DIGEST)


def test_a_tag_content_digest_moves_with_its_own_directory_alone():
    """#5338: C7's flux reference reads this, so a change to another tag must keep it."""
    before = _bundle(_tag("bento-pdf"), _tag("peanut"))
    other = _bundle(_tag("bento-pdf"), _tag("peanut", "changed"))
    mine = _bundle(_tag("bento-pdf", "changed"), _tag("peanut"))
    assert (
        _content(before, "bento-pdf") == json.loads(before[DIGESTS])["bento-pdf"]["own"]
    )
    assert _content(other, "bento-pdf") == _content(before, "bento-pdf")
    assert _content(mine, "bento-pdf") != _content(before, "bento-pdf")


def test_a_per_tag_kind_carries_no_content_digest():
    docs = list(yaml_fast.safe_load_all(_bundle(_pihole("a", "b"))["flux/pihole.yaml"]))
    assert CONTENT_DIGEST in docs[0]["metadata"]["annotations"]
    assert "annotations" not in docs[1]["metadata"]
