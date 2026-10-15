"""The fixture tree CI's `sops-manifest-type-proof` job applies (target slice D3, PR D3.3d).

The job itself needs a kind cluster, so it runs only in CI. These tests pin the offline half:
each manifest carries the SOPS types its half of the proof depends on. A green manifest that
SOPS recorded as an int would make the job's green half measure the red half's question.

Run: uv run pytest scripts/secrets_mgmt/tests/test_sops_type_proof_fixture.py
"""

from lib import yaml_fast

from _sops_type_proof import FIELDS, build, recorded_types


def test_the_built_halves_carry_str_and_int_and_the_kustomizations_decrypt(
    tmp_path,
):
    build(tmp_path / "out", "kind-registry:5000")
    out = tmp_path / "out"
    for half, kind in (("green", "str"), ("red", "int")):
        text = (out / "artifact" / half / "secret.sops.yaml").read_text()
        assert recorded_types(text) == dict.fromkeys(FIELDS, kind)
    docs = list(yaml_fast.safe_load_all((out / "flux-objects.yaml").read_text()))
    assert docs[0]["spec"]["url"] == "oci://kind-registry:5000/proof"
    assert [d["spec"]["path"] for d in docs[1:]] == ["./green", "./red"]
    assert all(d["spec"]["decryption"]["provider"] == "sops" for d in docs[1:])
    assert (out / "age.agekey").read_text().startswith("# created:")


def test_a_plaintext_field_records_no_type():
    text = "stringData:\n  zero_lead: '0123'\n  long_digits: ENC[AES256_GCM,data:x,type:str]\n"
    assert recorded_types(text) == {"zero_lead": None, "long_digits": "str"}
