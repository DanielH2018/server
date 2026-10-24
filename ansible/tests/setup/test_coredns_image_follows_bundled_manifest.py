"""Guard: the CoreDNS image the cluster runs is the one k3s bundles at `k3s_version` (H1.4).

WHY. The `coredns.yaml.skip` marker stops k3s applying its bundled CoreDNS addon, image
included, so the Deployment ran 1.14.4 through two k3s bumps that bundled 1.14.6 and 1.14.7
(#3257). Flux now applies the `coredns` role, and `coredns_image` in its defaults is the only
thing that moves the image. Nothing reads the staged `manifests/coredns.yaml` on the host any
more, so this test ties the pin to the manifest by an excerpt of the manifest's image line,
labelled with the k3s version it came from.

H6's version bump moves `k3s_version`, the excerpt and its label, the pin and any template
change together. This test fails on the first two of those when they disagree.

Run: uv run pytest ansible/tests/setup/test_coredns_image_follows_bundled_manifest.py
"""

import re

from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES, SYSTEM_UPGRADE_DEFAULTS

_COREDNS_DEFAULTS = K8S_ROLES / "coredns" / "defaults" / "main.yml"

# The k3s version the excerpt below was read from, and the image block of that version's
# manifests/coredns.yaml before k3s substitutes `%{SYSTEM_DEFAULT_REGISTRY}%`.
_EXCERPT_K3S_VERSION = "v1.37.1+k3s1"
_EXCERPT = """\
      containers:
      - name: coredns
        image: "%{SYSTEM_DEFAULT_REGISTRY}%rancher/mirrored-coredns-coredns:1.14.7"
        imagePullPolicy: IfNotPresent
"""

_IMAGE_LINE = re.compile(r'image:\s*"?([^"\s]*coredns[^"\s]*)"?')


def _bundled_image(excerpt: str) -> str:
    """The image k3s stages for `excerpt`: its `image:` value with the registry placeholder empty."""
    found = _IMAGE_LINE.search(excerpt)
    assert found, "the excerpt holds no CoreDNS image line"
    return found.group(1).replace("%{SYSTEM_DEFAULT_REGISTRY}%", "")


def problems(
    k3s_version: str, label: str, excerpt: str, pin: str, rendered_image: str
) -> list[str]:
    """Why the pin is not the bundled image, or an empty list."""
    out = []
    if label != k3s_version:
        out.append(
            f"the excerpt is labelled {label} but k3s_version is {k3s_version}: port the new "
            "manifest's image line into the excerpt, then move the pin"
        )
    if pin != _bundled_image(excerpt):
        out.append(
            f"coredns_image is {pin} but the excerpt's image is {_bundled_image(excerpt)}"
        )
    if rendered_image != pin:
        out.append(f"the rendered Deployment runs {rendered_image}, not {pin}")
    return out


def _live_inputs() -> tuple[str, str, str]:
    k3s_version = yaml_fast.safe_load(SYSTEM_UPGRADE_DEFAULTS.read_text())[
        "k3s_version"
    ]
    pin = yaml_fast.safe_load(_COREDNS_DEFAULTS.read_text())["coredns_image"]
    deployment = yaml_fast.safe_load(
        render_role_template("coredns", "deployment.yaml.j2")
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return k3s_version, pin, containers[0]["image"]


def test_the_pin_and_the_rendered_deployment_follow_the_bundled_manifest():
    k3s_version, pin, rendered_image = _live_inputs()
    assert (
        problems(k3s_version, _EXCERPT_K3S_VERSION, _EXCERPT, pin, rendered_image) == []
    )


def test_a_label_behind_k3s_version_is_flagged():
    # The RED case for a k3s bump that moved `k3s_version` and left the excerpt behind.
    got = problems("v1.38.0+k3s1", _EXCERPT_K3S_VERSION, _EXCERPT, "x:1", "x:1")
    assert any("labelled" in p for p in got)


def test_a_pin_that_differs_from_the_excerpt_is_flagged():
    image = _bundled_image(_EXCERPT)
    got = problems("v", "v", _EXCERPT, "rancher/mirrored-coredns-coredns:1.14.4", image)
    assert any("coredns_image is" in p for p in got)


def test_a_rendered_image_that_differs_from_the_pin_is_flagged():
    image = _bundled_image(_EXCERPT)
    got = problems("v", "v", _EXCERPT, image, "rancher/other:1")
    assert any("rendered Deployment" in p for p in got)


def test_the_registry_placeholder_is_stripped_as_k3s_stages_it():
    assert _bundled_image(_EXCERPT) == "rancher/mirrored-coredns-coredns:1.14.7"
