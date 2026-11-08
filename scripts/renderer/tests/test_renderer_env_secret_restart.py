"""A workload that reads a Secret through env is a restart target of its flux-owned tag (#7575).

Env resolves once at pod start, so a rotated value reaches the Secret and never the running
pods unless the pod template carries `checksum/restart`. A Secret mounted as a plain volume is
refreshed by the kubelet, so only `secretKeyRef` and `envFrom.secretRef` readers are in scope.
The module renders every flux-owned tag once, in placeholder mode, against a fixture `.source`
that records every secret key, and reads the rendered tree.

Run: uv run pytest scripts/renderer/tests/test_renderer_env_secret_restart.py
"""

import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from render_share import placeholder_renderer
from renderer import homelab_render
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX
from renderer.renderer_lib.restart import RESTART_ANNOTATION, pod_template

# secretKeyRef on an extra rollout, envFrom.secretRef, a DaemonSet, a workload rendered in a
# deferred directory, and redis, whose `restart_on: []` leaves it unstamped.
MEMBERS = frozenset(
    {
        ("karakeep", "karakeep-time-tagger"),
        ("code-server", "code-server"),
        ("crowdsec", "crowdsec-node-agent"),
        ("pihole", "pihole-2"),
        ("authelia", "authelia-redis"),
    }
)
# scrutiny and janitorr mount their Secret as a volume; pihole-exporter's tag renders no Secret.
NOT_MEMBERS = frozenset(
    {
        ("scrutiny", "scrutiny-influxdb"),
        ("janitorr", "janitorr"),
        ("pihole-exporter", "pihole-exporter"),
    }
)


def _documents(out: Path, tag: str) -> list[tuple[Path, dict]]:
    found = []
    for directory in (tag, tag + DEFERRED_SUFFIX):
        for path in sorted((out / "cluster" / directory).rglob("*.yaml")):
            for obj in yaml_fast.safe_load_all(path.read_text()):
                if isinstance(obj, dict):
                    found.append((path, obj))
    return found


def _reads_a_secret_through_env(template: dict) -> bool:
    spec = template.get("spec") or {}
    for container in [
        *(spec.get("containers") or []),
        *(spec.get("initContainers") or []),
    ]:
        if any(s.get("secretRef") for s in container.get("envFrom") or []):
            return True
        if any(
            (e.get("valueFrom") or {}).get("secretKeyRef")
            for e in container.get("env") or []
        ):
            return True
    return False


def env_secret_workloads(out: Path, tags: Iterable[str]) -> dict[tuple[str, str], bool]:
    """Each `(tag, workload)` reading a Secret through env, mapped to whether it is stamped.

    Only tags whose render, deferred directory included, holds a Secret are read.
    """
    found = {}
    for tag in sorted(tags):
        documents = _documents(out, tag)
        if not any(obj.get("kind") == "Secret" for _, obj in documents):
            continue
        for _, obj in documents:
            template = pod_template(obj)
            if template is None or not _reads_a_secret_through_env(template):
                continue
            annotations = (template.get("metadata") or {}).get("annotations") or {}
            found[(tag, obj["metadata"]["name"])] = bool(
                annotations.get(RESTART_ANNOTATION)
            )
    return found


def _unstamped(out: Path, tags: Iterable[str]) -> list[tuple[str, str]]:
    return sorted(
        k for k, stamped in env_secret_workloads(out, tags).items() if not stamped
    )


@pytest.fixture(scope="module")
def flux_tags() -> set[str]:
    return homelab_render.flux_owned_tags()


@pytest.fixture(scope="module")
def render(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("env-secret-restart")
    source = root / "cluster-vars.source"
    source.write_text(
        "".join(f"{key} {'d' * 64}\n" for key in homelab_render.secret_keys())
    )
    cluster_file = root / "cluster-vars.sops.env"
    cluster_file.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    out = root / "out"
    code = homelab_render.cluster(
        out,
        None,
        None,
        render=placeholder_renderer(),
        cluster_file=cluster_file,
        source_file=source,
    )
    assert code == RENDER_OK
    return out


def test_every_env_secret_workload_of_a_flux_owned_tag_is_stamped_is_clean(
    render, flux_tags
):
    found = env_secret_workloads(render, flux_tags)
    assert MEMBERS <= set(found)
    assert not NOT_MEMBERS & set(found)
    assert _unstamped(render, flux_tags) == []


def _strip_annotation(path: Path) -> None:
    text = path.read_text()
    kept = [line for line in text.splitlines(True) if RESTART_ANNOTATION not in line]
    assert len(kept) == len(text.splitlines()) - 1
    path.write_text("".join(kept))


@pytest.mark.parametrize(
    ("tag", "workload", "file"),
    [
        ("karakeep", "karakeep-time-tagger", "deployment-time-tagger.yaml"),
        ("code-server", "code-server", "deployment.yaml"),
    ],
    ids=["secretKeyRef", "envFrom-secretRef"],
)
def test_a_planted_env_secret_workload_without_the_annotation_is_flagged(
    render, flux_tags, tmp_path, tag, workload, file
):
    out = tmp_path / "out"
    shutil.copytree(render, out)
    _strip_annotation(out / "cluster" / tag / file)
    assert _unstamped(out, flux_tags) == [(tag, workload)]
