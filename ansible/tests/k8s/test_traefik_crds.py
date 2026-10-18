"""traefik's CRDs come from its in-tree copy, pinned, never from a URL.

D5 step 26's traefik bullet: no render may fetch a URL, so the role keeps upstream's CRD file
under `files/crds/` and the flip PR moves that file into the `traefik-crds` role, whose
bundle renders it (#5666). Until then the
role's task applies the same file. Four things keep the copy honest:

- the file hashes to `traefik_k8s_crd_sha256`, so a hand edit or a partial refresh fails;
- `traefik_k8s_crd_version` has the image's minor, so a minor image bump fails until someone
  refreshes the copy (a patch bump does not, as upstream changes no CRD in a patch);
- no task in `tasks/` names a URL, so the fetch the copy replaced cannot come back;
- the copy declares every CRD the "established" wait names, so the wait cannot time out on a
  kind the file dropped.

The copy sits in `files/crds/`, not `files/upstream/`, because the renderer attaches
`files/upstream/` to the tag's render and refuses a tag that vendors files while its render
holds a placeholder, which traefik's Secrets do.

Run: uv run pytest ansible/tests/k8s/test_traefik_crds.py
"""

import hashlib
import shutil
from pathlib import Path

from _helpers import ROLES, image_tag, leaf_tasks, load_defaults, load_tasks
from _role_census import role_task_files
from lib import yaml_fast

TRAEFIK = ROLES / "k8s" / "traefik"
COPY = Path("files") / "crds" / "kubernetes-crd-definition-v1.yml"
WAIT_TASK = "Wait for the Traefik CRDs to be established"


def _minor(version: str) -> str:
    return ".".join(version.lstrip("v").split(".")[:2])


def _waited_crds(role: Path) -> set[str]:
    tasks = leaf_tasks(load_tasks(role / "tasks" / "main.yml"))
    [wait] = [t for t in tasks if t.get("name") == WAIT_TASK]
    cmd = wait["ansible.builtin.command"]["cmd"]
    return {
        word.removeprefix("crd/") for word in cmd.split() if word.startswith("crd/")
    }


def declared_crds(role: Path = TRAEFIK) -> set[str]:
    """The `metadata.name` of every CustomResourceDefinition the copy declares."""
    docs = yaml_fast.safe_load_all((role / COPY).read_text())
    return {
        d["metadata"]["name"]
        for d in docs
        if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition"
    }


def crd_pin_problems(role: Path = TRAEFIK) -> list[str]:
    """Every way traefik's CRD copy and its pins disagree, one line each."""
    defaults = load_defaults(role)
    problems = []
    actual = hashlib.sha256((role / COPY).read_bytes()).hexdigest()
    if actual != defaults["traefik_k8s_crd_sha256"]:
        problems.append(
            f"{COPY} hashes {actual}, traefik_k8s_crd_sha256 records "
            f"{defaults['traefik_k8s_crd_sha256']}"
        )
    image = image_tag(defaults["traefik_k8s_image"])
    crd = str(defaults["traefik_k8s_crd_version"])
    if _minor(image) != _minor(crd):
        problems.append(
            f"traefik_k8s_image is {image} but the CRD copy is {crd}; refresh the copy"
        )
    for path in role_task_files(role):
        if "://" in path.read_text():
            problems.append(f"{path.relative_to(role)} names a URL")
    missing = _waited_crds(role) - declared_crds(role)
    if missing:
        problems.append(f"the wait names CRDs the copy lacks: {sorted(missing)}")
    return problems


def _fixture(tmp_path: Path) -> Path:
    role = tmp_path / "traefik"
    for sub in ("defaults", "tasks", "files/crds"):
        shutil.copytree(TRAEFIK / sub, role / sub)
    return role


def test_the_real_role_is_clean():
    # Non-vacuity: the copy holds upstream's ten kinds and the wait names three of them.
    assert {"ingressroutes.traefik.io", "middlewares.traefik.io"} <= _waited_crds(
        TRAEFIK
    )
    assert len(declared_crds()) >= 10
    assert crd_pin_problems() == []


def test_a_faithful_copy_is_clean(tmp_path):
    assert crd_pin_problems(_fixture(tmp_path)) == []


def test_a_one_byte_change_to_the_copy_is_flagged(tmp_path):
    role = _fixture(tmp_path)
    copy = role / COPY
    copy.write_bytes(copy.read_bytes() + b"\n")
    [problem] = crd_pin_problems(role)
    assert problem.startswith(f"{COPY} hashes ")


def test_a_minor_image_bump_without_a_refresh_is_flagged(tmp_path):
    role = _fixture(tmp_path)
    defaults = role / "defaults" / "main.yml"
    defaults.write_text(
        defaults.read_text().replace("image: traefik:v3.7.", "image: traefik:v3.8.", 1)
    )
    [problem] = crd_pin_problems(role)
    assert "refresh the copy" in problem


def test_a_patch_image_bump_is_clean(tmp_path):
    role = _fixture(tmp_path)
    defaults = role / "defaults" / "main.yml"
    text = defaults.read_text()
    tag = image_tag(load_defaults(role)["traefik_k8s_image"])
    defaults.write_text(text.replace(f"traefik:{tag}", "traefik:v3.7.99", 1))
    assert crd_pin_problems(role) == []


def test_a_task_that_fetches_a_url_is_flagged(tmp_path):
    role = _fixture(tmp_path)
    tasks = role / "tasks" / "main.yml"
    tasks.write_text(
        tasks.read_text().replace(
            "{{ role_path }}/files/crds/kubernetes-crd-definition-v1.yml",
            "https://raw.githubusercontent.com/traefik/traefik/v3.7/x.yml",
        )
    )
    assert crd_pin_problems(role) == ["tasks/main.yml names a URL"]


def test_a_wait_on_a_crd_the_copy_lacks_is_flagged(tmp_path):
    role = _fixture(tmp_path)
    tasks = role / "tasks" / "main.yml"
    tasks.write_text(
        tasks.read_text().replace(
            "crd/tlsoptions.traefik.io", "crd/tlsoptions.traefik.io crd/gone.traefik.io"
        )
    )
    assert crd_pin_problems(role) == [
        "the wait names CRDs the copy lacks: ['gone.traefik.io']"
    ]
