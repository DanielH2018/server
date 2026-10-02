"""flaresolverr's media loading stays a flippable default, not a manifest literal.

DISABLE_MEDIA stops the headless browser fetching images, CSS and other media while it solves a
Cloudflare challenge. Its failure mode lands on the indexers, not on this pod: a
challenge type that needs an image or a stylesheet stops solving, and the operator sees Prowlarr
indexers go red. The revert therefore has to be one edit to
`ansible/roles/k8s/prowlarr/defaults/main.yml:prowlarr_k8s_fs_disable_media`, with the rationale
beside it, rather than a hunt through the manifest for a hardcoded string.

This guard fails if a future edit inlines the value, drops the env var, or renders it as a YAML
boolean — the Kubernetes API rejects a non-string env value, so an unquoted `true` breaks the
apply rather than the render.
"""

from lib import yaml_fast

from _k8s_render import render_role_template, rendered_docs

_TEMPLATE = "deployment-flaresolverr.yaml.j2"


def _env_of(docs) -> dict:
    for doc in docs:
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "flaresolverr":
                return {e["name"]: e["value"] for e in container["env"]}
    raise AssertionError(
        f"no flaresolverr container rendered from prowlarr/{_TEMPLATE}"
    )


def _flaresolverr_env():
    return _env_of(
        doc
        for role, tpl, doc in rendered_docs()
        if role == "prowlarr" and tpl == _TEMPLATE
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
    """The revert path itself: flipping the default flips the rendered value.

    Rendered at `false` rather than read off the template, so an alias between the default and
    the env line still counts as reading it, and a literal `"true"` fails.
    """
    rendered = render_role_template(
        "prowlarr", _TEMPLATE, {"prowlarr_k8s_fs_disable_media": False}
    )
    env = _env_of(d for d in yaml_fast.safe_load_all(rendered) if d)
    assert env.get("DISABLE_MEDIA") == "false", (
        f"{_TEMPLATE} rendered DISABLE_MEDIA={env.get('DISABLE_MEDIA')!r} with "
        "prowlarr_k8s_fs_disable_media false — reverting an indexer regression now means "
        "editing a manifest, not a default"
    )
