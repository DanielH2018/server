"""The fixture tree for CI's `sops-manifest-type-proof` job (target slice D3, PR D3.3d).

R2 PR 7b moves `mullvad_account`, whose value is all digits, into a SOPS manifest that
kustomize-controller decrypts and applies. Nothing offline shows what the controller and the
API server do with a decrypted all-digit value in a quoted `stringData` field. The job
measures it in a throwaway kind cluster, and this module builds what the job applies:

- `artifact/green/` holds the manifest `cluster_vars.py write` produces from a fixture
  `secrets.yml` whose values are `0123` and a 16-digit value above 2**53, which a float64
  detour would change. `write` runs end to end, so the proof exercises its own manifest code.
- `artifact/red/` holds the same Secret encrypted from ints, so SOPS records `type:int`. The
  job requires it to fail to apply or to come back changed; `0123` has no int form, so the
  16-digit value is the one the job judges.
- `flux-objects.yaml` holds one OCIRepository and a Kustomization with `decryption` for each
  half, on separate paths of one artifact, so red's failure cannot block green's apply.
- `sops/keys/cluster.txt` is the age key the job loads into the `sops-age` Secret.

Every key is generated per run by `_cluster_vars_fixture.make_sops_tree`. Nothing here reads
the real `secrets.yml` or the real cluster files.

Run (the job's form): PYTHONPATH=scripts:scripts/secrets_mgmt/tests \
    uv run python scripts/secrets_mgmt/tests/_sops_type_proof.py --out DIR --registry HOST:PORT
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

from _cluster_vars_fixture import make_sops_tree
from lib import yaml_fast
from secrets_mgmt.cluster_vars import encrypt, write
from secrets_mgmt.cluster_vars_check import Expected, ManifestEntry

NAMESPACE = "proof"

# The digits each `stringData` field must decode to. 2**53 + 1 is the smallest integer a
# float64 cannot hold, so a JSON number detour turns it into ...992.
VALUES = {"proof_zero_lead": "0123", "proof_long_digits": "9007199254740993"}
FIELDS = {"zero_lead": "proof_zero_lead", "long_digits": "proof_long_digits"}

# `write` encrypts the cluster dotenv whole when it is absent, and an empty dotenv is not one
# SOPS writes, so the fixture carries one plain key besides the manifest's two.
PLAIN = {"proof_plain": "fixture-plain-q4z"}

TEMPLATE = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: proof-green\n"
    f"  namespace: {NAMESPACE}\ntype: Opaque\nstringData:\n"
    + "".join(
        f'  {field}: "HOMELAB_SECRET__{key}__END"\n' for field, key in FIELDS.items()
    )
)
ENTRY = ManifestEntry("proof", "green-secret.yaml", TEMPLATE, frozenset(VALUES))
EXPECTED = Expected(keys=frozenset(PLAIN), standalone=frozenset(), manifests=(ENTRY,))

_TYPE = re.compile(r"^ENC\[AES256_GCM,.*,type:(\w+)\]$")


def recorded_types(ciphertext: str) -> dict[str, str | None]:
    """The `type:` SOPS recorded for each `stringData` field, by field name.

    A field that is not a SOPS ciphertext maps to None.
    """
    fields = yaml_fast.safe_load(ciphertext).get("stringData") or {}
    return {
        name: (m.group(1) if (m := _TYPE.match(str(value))) else None)
        for name, value in fields.items()
    }


def require_types(half: str, ciphertext: str, kind: str) -> None:
    """Refuse a manifest unless every `stringData` field records `type:<kind>`.

    Raises:
        SystemExit: A field records another type or none, so the half would measure the
            other half's question.
    """
    types = recorded_types(ciphertext)
    if types != dict.fromkeys(FIELDS, kind):
        raise SystemExit(f"the {half} manifest records {types}, not type:{kind}")


def flux_objects(registry: str) -> str:
    """The OCIRepository and the two Kustomizations, as one multi-document YAML."""
    docs = [
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "OCIRepository",
            "metadata": {"name": "proof", "namespace": "flux-system"},
            "spec": {
                "interval": "1m",
                "url": f"oci://{registry}/proof",
                "insecure": True,
                "ref": {"tag": "proof"},
            },
        }
    ]
    for half in ("green", "red"):
        docs.append(
            {
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                "kind": "Kustomization",
                "metadata": {"name": f"proof-{half}", "namespace": "flux-system"},
                "spec": {
                    "interval": "1m",
                    "retryInterval": "15s",
                    "timeout": "1m",
                    "prune": False,
                    "path": f"./{half}",
                    "sourceRef": {"kind": "OCIRepository", "name": "proof"},
                    "decryption": {
                        "provider": "sops",
                        "secretRef": {"name": "sops-age"},
                    },
                },
            }
        )
    return yaml.safe_dump_all(docs, sort_keys=False)


def build(out: Path, registry: str) -> None:
    """Write the fixture tree under `out`.

    Raises:
        SystemExit: `write` failed, or a manifest does not carry the SOPS types its half
            depends on. Either way the job would measure something other than its question.
    """
    sops_tree = make_sops_tree(out / "sops", {**PLAIN, **VALUES})
    tree = sops_tree.tree
    if write(tree, EXPECTED, sops_tree.run) != 0:
        raise SystemExit("cluster_vars write failed on the fixture tree")
    green = (tree.manifests / ENTRY.name).read_text()

    red_obj = yaml_fast.safe_load(TEMPLATE)
    red_obj["metadata"]["name"] = "proof-red"
    red_obj["stringData"] = {field: int(VALUES[key]) for field, key in FIELDS.items()}
    red = encrypt(
        tree,
        yaml.safe_dump(red_obj, sort_keys=False),
        tree.manifests / "proof.red-secret.sops.yaml",
        sops_tree.run,
    )

    want = {"green": ("str", green), "red": ("int", red)}
    for half, (kind, text) in want.items():
        require_types(half, text, kind)
        directory = out / "artifact" / half
        directory.mkdir(parents=True)
        (directory / "secret.sops.yaml").write_text(text)
        (directory / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources:\n  - secret.sops.yaml\n"
        )
    (out / "flux-objects.yaml").write_text(flux_objects(registry))
    shutil.copy(out / "sops" / "keys" / "cluster.txt", out / "age.agekey")
    print(f"built {out}: green type:str, red type:int, fields {', '.join(FIELDS)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="an absent directory")
    parser.add_argument(
        "--registry", required=True, help="HOST:PORT source-controller pulls from"
    )
    args = parser.parse_args(argv)
    build(args.out, args.registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
