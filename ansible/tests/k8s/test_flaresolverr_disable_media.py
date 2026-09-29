"""flaresolverr's media loading stays a flippable default, not a manifest literal.

DISABLE_MEDIA stops the headless browser fetching images, CSS and other media while it solves a
Cloudflare challenge (#2893). Its failure mode lands on the indexers, not on this pod: a
challenge type that needs an image or a stylesheet stops solving, and the operator sees Prowlarr
indexers go red. The revert therefore has to be one edit to
`ansible/roles/k8s/prowlarr/defaults/main.yml:prowlarr_k8s_fs_disable_media`, with the rationale
beside it, rather than a hunt through the manifest for a hardcoded string.

This guard fails if a future edit inlines the value, drops the env var, or renders it as a YAML
boolean — the Kubernetes API rejects a non-string env value, so an unquoted `true` breaks the
apply rather than the render.
"""

from _k8s_render import rendered_docs
from lib.repo_paths import ROLES

_TEMPLATE = "deployment-flaresolverr.yaml.j2"


def _flaresolverr_env():
    for role, tpl, doc in rendered_docs():
        if role != "prowlarr" or tpl != _TEMPLATE:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "flaresolverr":
                return {e["name"]: e["value"] for e in container["env"]}
    raise AssertionError(
        f"no flaresolverr container rendered from prowlarr/{_TEMPLATE}"
    )


def test_disable_media_renders_as_a_quoted_string_from_the_default():
    env = _flaresolverr_env()
    assert "DISABLE_MEDIA" in env, (
        "flaresolverr lost DISABLE_MEDIA — each solve loads media again, which is the "
        "per-browser cost prowlarr_k8s_fs_mem_limit was raised to absorb (#2893)"
    )
    assert env["DISABLE_MEDIA"] == "true", (
        f"DISABLE_MEDIA rendered {env['DISABLE_MEDIA']!r}; FlareSolverr reads the literal "
        "strings 'true'/'false', and a Kubernetes env value must be a string"
    )


def test_the_value_is_not_hardcoded_in_the_manifest():
    source = ROLES / "k8s" / "prowlarr" / "templates" / _TEMPLATE
    assert "prowlarr_k8s_fs_disable_media" in source.read_text(), (
        f"{_TEMPLATE} no longer renders DISABLE_MEDIA from prowlarr_k8s_fs_disable_media — "
        "reverting an indexer regression now means editing a manifest, not a default"
    )
