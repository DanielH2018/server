"""`slskd_k8s_share_library` is the one switch that shares the music library with Soulseek.

On, slskd shares a read-only mount of media/music; off, it shares nothing and mounts nothing of
the library. The library mount is read-only either way, so slskd can never alter what it shares.
"""

import pytest

from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.render_context import render_context
from validate.k8s_manifests import (
    SHARED_TPL,
    k8s_entries,
    make_env,
    make_lookup,
    register_ansible_filters,
)

ROLE = K8S_ROLES / "slskd"


def _slskd_container(share: bool) -> dict:
    ctx = render_context(
        ROLE,
        overrides={
            "container_item": k8s_entries()["slskd"],
            "slskd_k8s_share_library": share,
        },
        strict=True,
    )
    env = make_env([ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    [deployment] = [
        d
        for d in yaml_fast.safe_load_all(
            env.get_template("deployment.yaml.j2").render(**ctx)
        )
        if d
    ]
    [slskd] = [
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "slskd"
    ]
    return slskd


def _shared_dirs(container: dict) -> list[str]:
    return [e["value"] for e in container["env"] if e["name"] == "SLSKD_SHARED_DIR"]


def _library_mounts(container: dict) -> list[dict]:
    return [m for m in container["volumeMounts"] if m.get("subPath") == "media/music"]


def test_sharing_on_shares_a_read_only_music_mount():
    slskd = _slskd_container(share=True)
    assert _shared_dirs(slskd) == ["/music"]
    [mount] = _library_mounts(slskd)
    assert mount["mountPath"] == "/music"
    assert mount["readOnly"] is True


def test_sharing_off_shares_and_mounts_nothing_of_the_library():
    slskd = _slskd_container(share=False)
    assert _shared_dirs(slskd) == []
    assert _library_mounts(slskd) == []


@pytest.mark.parametrize("share", [True, False])
def test_slskd_never_mounts_the_whole_media_volume(share):
    """Downloads need torrents/ alone; the library is never writable from this pod."""
    slskd = _slskd_container(share)
    media = [m for m in slskd["volumeMounts"] if m["name"] == "media"]
    assert all(m.get("subPath") for m in media), media
