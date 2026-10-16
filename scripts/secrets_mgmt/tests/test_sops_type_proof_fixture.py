"""The fixture tree CI's `sops-manifest-type-proof` job applies (target slice D3, PR D3.3d).

The job itself needs a kind cluster, so it runs only in CI. These tests pin the builder's own
guard: each manifest must carry the SOPS type its half of the proof depends on. A green
manifest that SOPS recorded as an int would make the job's green half measure the red half's
question.

Run: uv run pytest scripts/secrets_mgmt/tests/test_sops_type_proof_fixture.py
"""

import pytest

from _sops_type_proof import build, require_types


def _manifest(zero_lead: str, long_digits: str) -> str:
    return f"stringData:\n  zero_lead: {zero_lead}\n  long_digits: {long_digits}\n"


INT = "ENC[AES256_GCM,data:x,iv:y,tag:z,type:int]"
STR = "ENC[AES256_GCM,data:x,iv:y,tag:z,type:str]"


def test_write_produces_a_green_manifest_the_type_guard_accepts(tmp_path):
    # build() runs require_types on both halves, so returning at all is the assertion.
    build(tmp_path / "out", "kind-registry:5000")
    for half in ("green", "red"):
        assert (tmp_path / "out" / "artifact" / half / "secret.sops.yaml").is_file()


def test_a_manifest_carrying_its_halfs_type_is_clean():
    require_types("green", _manifest(STR, STR), "str")
    require_types("red", _manifest(INT, INT), "int")


@pytest.mark.parametrize(
    "text",
    [_manifest(INT, INT), _manifest(STR, INT), _manifest("'0123'", STR)],
    ids=["both-int", "one-int", "one-plaintext"],
)
def test_a_green_manifest_not_typed_str_throughout_is_flagged(text):
    with pytest.raises(SystemExit, match="the green manifest records"):
        require_types("green", text, "str")
