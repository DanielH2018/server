"""The role-owned kind that applies before `<tag>`: `templates/precheck/` into `<tag>-precheck` (#5719).

A role ships a pre-apply check, such as pihole's sibling guard, as `templates/precheck/`. The
renderer writes it to `cluster/<tag>-precheck/` and its own Kustomization, which `<tag>`
depends on, so a failing check holds the tag. The Job census admits a `precheck` Job there,
and `check-bundle` allows the Kustomization only for a role that ships the directory. The
other kinds' ordering tests are in test_renderer_bundle_ordering.py.

Run: uv run pytest scripts/renderer/tests/test_renderer_precheck.py
"""

import re
import shutil

from _job_census import HELPER, JOB_KIND, problems

from deploy_tools import bundle_census
from lib import yaml_fast
from lib.exit_codes import FAILED, OK, RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import (
    CLUSTER_VARS_FILE,
    SHARED_KINDS,
    bundle_files,
    reserved_tags,
)
from renderer.renderer_lib.digest8 import configmap_digest8, job_spec_digest8
from renderer.renderer_lib.ordering import (
    ANSIBLE_OWNED_DEPS,
    TAG_KIND_SUFFIXES,
    Ordering,
    kustomization_edges,
    split_kind,
)
from renderer.renderer_lib.placeholders import render_placeholders

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
TAG = "littlelink"
PRECHECK = f"{TAG}-precheck"
CHECK_TEMPLATE = (
    "kind: ConfigMap\nmetadata:\n  name: {{ manifests_service }}-precheck\n"
    "  namespace: default\n"
)


def _precheck_bundle(backoff: int) -> dict[str, list[dict]]:
    """`sonarr-precheck` holding a precheck Job, read-only, and the script ConfigMap it runs."""
    configmap = {
        "kind": "ConfigMap",
        "metadata": {"name": "sonarr-precheck-scripts"},
        "data": {
            "run.py": "import json\n\njson.loads('{}')\n",
            HELPER.name: HELPER.read_text(),
        },
    }
    configmap["metadata"]["name"] += f"-{configmap_digest8(configmap)}"
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "sonarr-precheck", "annotations": {JOB_KIND: "precheck"}},
        "spec": {
            "backoffLimit": backoff,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "run",
                            "image": "python",
                            "command": ["python3", "/scripts/run.py"],
                            "volumeMounts": [
                                {"name": "scripts", "mountPath": "/scripts"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "scripts",
                            "configMap": {"name": configmap["metadata"]["name"]},
                        }
                    ],
                }
            },
        },
    }
    job["metadata"]["name"] += f"-{job_spec_digest8(job)}"
    return {"sonarr-precheck": [job, configmap]}


def test_a_compliant_precheck_job_in_its_directory_is_clean():
    """`<tag>-precheck/` admits a `precheck` Job, and its `backoffLimit` is 0."""
    bundle = _precheck_bundle(0)
    assert [doc["kind"] for doc in bundle["sonarr-precheck"]] == ["Job", "ConfigMap"]
    assert problems(bundle) == []


def test_a_precheck_job_that_retries_is_flagged():
    rows = {line.split(":", 1)[0] for line in problems(_precheck_bundle(1))}
    assert rows == {"backoff"}


def test_a_rendered_precheck_directory_applies_before_its_tag_and_passes_check_bundle(
    tmp_path, capsys
):
    """The renderer writes `<tag>-precheck` from the role's input, and `<tag>` waits on it.

    The same bundle checked against the tree's littlelink, which ships no precheck manifest,
    fails, so the census allows the kind only for the role whose input calls for it.
    """
    roles = tmp_path / "roles"
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    precheck = roles / TAG / "templates" / "precheck"
    precheck.mkdir()
    (precheck / "precheck-check.yaml.j2").write_text(CHECK_TEMPLATE)
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster_file,
        render=lambda tags, keys: render_placeholders(tags, keys, roles=roles),
        vendored=roles,
    )
    assert code == RENDER_OK
    rendered = out / "cluster" / PRECHECK
    assert rendered.is_dir(), f"no cluster/{PRECHECK}/ was rendered"
    assert sorted(p.name for p in rendered.iterdir()) == [
        "kustomization.yaml",
        "precheck-check.yaml",
    ]
    assert f"name: {PRECHECK}" in (rendered / "precheck-check.yaml").read_text()
    docs = {
        d["metadata"]["name"]: d
        for d in yaml_fast.safe_load_all((out / "flux" / f"{TAG}.yaml").read_text())
    }
    assert set(docs) == {TAG, PRECHECK}
    assert {"name": PRECHECK} in docs[TAG]["spec"]["dependsOn"]
    assert TAG not in [e["name"] for e in docs[PRECHECK]["spec"].get("dependsOn", [])]

    assert bundle_census.report(out, [TAG], roles) == OK
    capsys.readouterr()
    assert bundle_census.report(out, [TAG]) == FAILED


# -- the ordering (D4.2's rules, applied to the new kind) -------------------------------------

TIMEOUT = "600s"
CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: x\n  namespace: homelab\n"


def _docs(ordering: Ordering, tags, directories) -> dict[str, dict]:
    """Every tag Kustomization `bundle_files` writes for `tags`, by name."""
    files = bundle_files(directories, {}, tags, TIMEOUT, DOTENV, (), ordering)
    return {
        doc["metadata"]["name"]: doc
        for tag in tags
        for doc in yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"])
    }


def _depends(doc: dict) -> list[str]:
    return [edge["name"] for edge in doc["spec"].get("dependsOn", [])]


def _dropped(doc: dict) -> str | None:
    return (doc["metadata"].get("annotations") or {}).get(ANSIBLE_OWNED_DEPS)


def _every_kind() -> dict:
    """Directories for `pihole` with every per-tag kind but the precheck, and a `bento-pdf`."""
    files = {"x.yaml": CONFIGMAP}
    names = [
        "pihole",
        "pihole-deferred",
        "pihole-snapshot",
        "pihole-verify",
        "bento-pdf",
    ]
    return {name: files for name in names}


PIHOLE_PRECHECK = "pihole-precheck"
# A guard Job shaped as pihole's: its deadline is the one the precheck timeout must outlast.
GUARD_JOB = (
    "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: guard\n  namespace: {namespace}\n"
    "spec:\n  backoffLimit: 0\n  activeDeadlineSeconds: {deadline}\n"
)


def _prechecking(namespace: str = "homelab", deadline: int = 420) -> dict:
    """`_every_kind`'s directories plus a `pihole-precheck` holding one guard Job."""
    job = GUARD_JOB.format(namespace=namespace, deadline=deadline)
    return _every_kind() | {PIHOLE_PRECHECK: {"guard.yaml": job}}


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert match, f"{duration!r} is not a duration of h, m and s"
    hours, minutes, secs = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(secs or 0)


def test_a_precheck_kind_inherits_its_tags_edges_and_the_tag_waits_on_it():
    ordering = Ordering(deps={"pihole": ["bento-pdf", "traefik"]})
    docs = _docs(ordering, ["bento-pdf", "pihole"], _prechecking())
    assert PIHOLE_PRECHECK in _depends(docs["pihole"])
    assert "bento-pdf" in _depends(docs["pihole"])
    precheck = docs.get(PIHOLE_PRECHECK)
    assert precheck is not None, "flux/pihole.yaml holds no pihole-precheck"
    assert _depends(precheck) == ["bento-pdf"]
    assert _dropped(precheck) == "traefik"


def test_a_precheck_kind_follows_its_tag_in_suffix_order():
    files = bundle_files(_prechecking(), {}, ["bento-pdf", "pihole"], TIMEOUT, DOTENV)
    names = [
        d["metadata"]["name"]
        for d in yaml_fast.safe_load_all(files["flux/pihole.yaml"])
    ]
    assert PIHOLE_PRECHECK in names
    held = set(_prechecking())
    assert names == ["pihole"] + [
        "pihole" + suffix for suffix in TAG_KIND_SUFFIXES if "pihole" + suffix in held
    ]
    precheck = next(
        d
        for d in yaml_fast.safe_load_all(files["flux/pihole.yaml"])
        if d["metadata"]["name"] == PIHOLE_PRECHECK
    )
    assert precheck["spec"]["path"] == f"./cluster/{PIHOLE_PRECHECK}"
    assert precheck["spec"]["deletionPolicy"] == "Orphan"


def test_split_kind_reads_a_precheck_directory_as_its_tags():
    assert split_kind(PIHOLE_PRECHECK) == ("pihole", "-precheck")


def test_a_tag_ending_in_the_precheck_suffix_takes_a_reserved_name():
    assert reserved_tags(["fixture-precheck"], SHARED_KINDS) == ["fixture-precheck"]


def test_the_precheck_timeout_outlasts_its_guard_jobs_deadline():
    """The default sits below the guard's 420s, so only a timeout read from the Job passes."""
    files = bundle_files(_prechecking(), {}, ["bento-pdf", "pihole"], "300s", DOTENV)
    docs = {
        d["metadata"]["name"]: d
        for d in yaml_fast.safe_load_all(files["flux/pihole.yaml"])
    }
    assert PIHOLE_PRECHECK in docs, "flux/pihole.yaml holds no pihole-precheck"
    assert _seconds(docs[PIHOLE_PRECHECK]["spec"]["timeout"]) > 420


def test_a_precheck_document_in_a_role_created_namespace_waits_on_that_role():
    """The kind applies before `<tag>`, so its namespace must come from somewhere else."""
    directories = _prechecking(namespace="observability")
    ordering = Ordering(
        namespace_roles={"observability": "observability"},
        flux_owned=frozenset({"pihole", "observability"}),
    )
    edges = kustomization_edges(["pihole"], directories, ordering)
    precheck = edges.get(PIHOLE_PRECHECK)
    assert precheck is not None, "the renderer writes no edges for pihole-precheck"
    assert "observability" in precheck.kept
