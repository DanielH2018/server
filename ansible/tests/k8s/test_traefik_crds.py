"""traefik's CRDs come from the traefik-crds role's in-tree copy, pinned, never from a URL.

D5 step 26's traefik bullet: no render may fetch a URL, so `traefik-crds` keeps upstream's
CRD file under `files/upstream/`, and the renderer attaches it to that tag's bundle verbatim
(#5666). `scripts/dev/vendor_manifest.py --check` holds the file to its sha256 pin
(`ansible/tests/k8s/test_vendored_manifests_match_their_pins.py`). Two more things keep
the copy honest:

- `traefik_crds_k8s_version` has traefik's image minor, so a minor image bump fails until
  someone refreshes the copy (a patch bump does not, as upstream changes no CRD in a patch);
- the copy declares every kind traefik's own render instantiates, so a refresh that dropped
  one fails here rather than as a Kustomization that cannot apply.

Run: uv run pytest ansible/tests/k8s/test_traefik_crds.py
"""

import shutil
from pathlib import Path

from _helpers import ROLES, image_tag, load_defaults
from _k8s_render import rendered_docs
from lib import yaml_fast

TRAEFIK = ROLES / "k8s" / "traefik"
TRAEFIK_CRDS = ROLES / "k8s" / "traefik-crds"
COPY = Path("files") / "upstream" / "kubernetes-crd-definition-v1.yml"
GROUP = "traefik.io"


def _minor(version: str) -> str:
    return ".".join(version.lstrip("v").split(".")[:2])


def declared_crds(role: Path = TRAEFIK_CRDS) -> set[str]:
    """The `metadata.name` of every CustomResourceDefinition the copy declares."""
    docs = yaml_fast.safe_load_all((role / COPY).read_text())
    return {
        d["metadata"]["name"]
        for d in docs
        if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition"
    }


def used_crds() -> set[str]:
    """`<plural>.traefik.io` for every traefik.io kind traefik's own render instantiates."""
    return {
        f"{doc['kind'].lower()}s.{GROUP}"
        for role, _tpl, doc in rendered_docs()
        if role == "traefik" and str(doc.get("apiVersion", "")).startswith(f"{GROUP}/")
    }


def crd_pin_problems(
    crds: Path = TRAEFIK_CRDS, traefik: Path = TRAEFIK, used: set[str] | None = None
) -> list[str]:
    """Every way the CRD copy, its pins and traefik's image disagree, one line each."""
    defaults = load_defaults(crds)
    problems = []
    image = image_tag(load_defaults(traefik)["traefik_k8s_image"])
    crd = str(defaults["traefik_crds_k8s_version"])
    if _minor(image) != _minor(crd):
        problems.append(
            f"traefik_k8s_image is {image} but the CRD copy is {crd}; refresh the copy"
        )
    missing = (used_crds() if used is None else used) - declared_crds(crds)
    if missing:
        problems.append(f"traefik renders kinds the copy lacks: {sorted(missing)}")
    return problems


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    crds, traefik = tmp_path / "traefik-crds", tmp_path / "traefik"
    for sub in ("defaults", "files/upstream"):
        shutil.copytree(TRAEFIK_CRDS / sub, crds / sub)
    shutil.copytree(TRAEFIK / "defaults", traefik / "defaults")
    return crds, traefik


def test_the_real_roles_are_clean():
    # Non-vacuity: the copy holds upstream's ten kinds, and traefik renders at least the
    # IngressRoute, Middleware and TLSOption kinds from it.
    assert {
        "ingressroutes.traefik.io",
        "middlewares.traefik.io",
        "tlsoptions.traefik.io",
    } <= used_crds()
    assert len(declared_crds()) >= 10
    assert crd_pin_problems() == []


def test_a_faithful_copy_is_clean(tmp_path):
    assert crd_pin_problems(*_fixture(tmp_path)) == []


def test_a_minor_image_bump_without_a_refresh_is_flagged(tmp_path):
    crds, traefik = _fixture(tmp_path)
    defaults = traefik / "defaults" / "main.yml"
    defaults.write_text(
        defaults.read_text().replace("image: traefik:v3.7.", "image: traefik:v3.8.", 1)
    )
    [problem] = crd_pin_problems(crds, traefik)
    assert "refresh the copy" in problem


def test_a_patch_image_bump_is_clean(tmp_path):
    crds, traefik = _fixture(tmp_path)
    defaults = traefik / "defaults" / "main.yml"
    tag = image_tag(load_defaults(traefik)["traefik_k8s_image"])
    defaults.write_text(
        defaults.read_text().replace(f"traefik:{tag}", "traefik:v3.7.99", 1)
    )
    assert crd_pin_problems(crds, traefik) == []


def test_a_kind_the_copy_lacks_is_flagged(tmp_path):
    crds, traefik = _fixture(tmp_path)
    used = used_crds() | {"gones.traefik.io"}
    assert crd_pin_problems(crds, traefik, used) == [
        "traefik renders kinds the copy lacks: ['gones.traefik.io']"
    ]
