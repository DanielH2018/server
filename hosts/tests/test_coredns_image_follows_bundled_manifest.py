"""Guard: the CoreDNS image the cluster runs is the one k3s bundles at `k3s_version` (H1.4).

WHY. The `coredns.yaml.skip` marker stops k3s applying its bundled CoreDNS addon, image
included, so the Deployment ran 1.14.4 through two k3s bumps that bundled 1.14.6 and 1.14.7
(#3257). Flux now applies the `coredns` role, and `coredns_image` in its defaults is the only
thing that moves the image. Nothing reads the staged `manifests/coredns.yaml` on the host any
more, so this test ties the pin to the manifest by an excerpt of the manifest's image line,
labelled with the k3s version it came from.

H6's version bump moves `k3s_version`, the excerpt and its label, the pin and any template
change together. This test fails on the first two of those when they disagree.

Run: uv run pytest hosts/tests/test_coredns_image_follows_bundled_manifest.py
"""

import re

from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES, SYSTEM_UPGRADE_DEFAULTS

_COREDNS_DEFAULTS = K8S_ROLES / "coredns" / "defaults" / "main.yml"

# The k3s version the excerpt below was read from, and the image block of that version's
# manifests/coredns.yaml before k3s substitutes `%{SYSTEM_DEFAULT_REGISTRY}%`.
_EXCERPT_K3S_VERSION = "v1.37.2+k3s1"
_EXCERPT = """\
      containers:
      - name: coredns
        image: "%{SYSTEM_DEFAULT_REGISTRY}%rancher/mirrored-coredns-coredns:1.14.7"
        imagePullPolicy: IfNotPresent
"""

# The index digest of each image an excerpt has named, read from the registry by hand
# (docs/k3s-upgrade.md). Keyed by image, so an excerpt naming a new tag fails until its
# digest is recorded here.
_DIGESTS = {
    "rancher/mirrored-coredns-coredns:1.14.7": "sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286",
}

_IMAGE_LINE = re.compile(r'image:\s*"?([^"\s]*coredns[^"\s]*)"?')


def _bundled_image(excerpt: str) -> str:
    """The image k3s stages for `excerpt`: its `image:` value with the registry placeholder empty."""
    found = _IMAGE_LINE.search(excerpt)
    assert found, "the excerpt holds no CoreDNS image line"
    return found.group(1).replace("%{SYSTEM_DEFAULT_REGISTRY}%", "")


def problems(
    k3s_version: str,
    label: str,
    excerpt: str,
    pin: str,
    rendered_image: str,
    digests: dict[str, str] = _DIGESTS,
) -> list[str]:
    """Why the pin is not the bundled image at its recorded digest, or an empty list."""
    out = []
    if label != k3s_version:
        out.append(
            f"the excerpt is labelled {label} but k3s_version is {k3s_version}: port the new "
            "manifest's image line into the excerpt, then move the pin"
        )
    image = _bundled_image(excerpt)
    if image not in digests:
        out.append(
            f"no digest is recorded for {image}: add the tag's index digest to _DIGESTS "
            "(docs/k3s-upgrade.md has the command)"
        )
    elif pin != f"{image}@{digests[image]}":
        out.append(
            f"coredns_image is {pin} but the excerpt's image is {image}@{digests[image]}"
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


def test_the_bundled_tag_at_its_recorded_digest_is_clean():
    image = _bundled_image(_EXCERPT)
    pin = f"{image}@{_DIGESTS[image]}"
    assert problems("v", "v", _EXCERPT, pin, pin) == []


def test_the_bundled_tag_at_another_digest_is_flagged():
    pin = f"{_bundled_image(_EXCERPT)}@sha256:{'a' * 64}"
    got = problems("v", "v", _EXCERPT, pin, pin)
    assert any("coredns_image is" in p for p in got)


def test_the_bundled_tag_without_a_digest_is_flagged():
    pin = _bundled_image(_EXCERPT)
    got = problems("v", "v", _EXCERPT, pin, pin)
    assert any("coredns_image is" in p for p in got)


def test_an_excerpt_tag_with_no_recorded_digest_is_flagged():
    # The RED case for a k3s bump that moved the excerpt and the pin's tag, not the digest.
    excerpt = _EXCERPT.replace("1.14.7", "1.14.8")
    pin = (
        f"rancher/mirrored-coredns-coredns:1.14.8@{_DIGESTS[_bundled_image(_EXCERPT)]}"
    )
    got = problems("v", "v", excerpt, pin, pin)
    assert any("no digest is recorded" in p for p in got)


def test_a_digest_on_the_pin_with_another_tag_is_flagged():
    pin = f"rancher/mirrored-coredns-coredns:1.14.4@sha256:{'a' * 64}"
    got = problems("v", "v", _EXCERPT, pin, pin)
    assert any("coredns_image is" in p for p in got)


def test_a_rendered_image_that_differs_from_the_pin_is_flagged():
    image = _bundled_image(_EXCERPT)
    got = problems("v", "v", _EXCERPT, image, "rancher/other:1")
    assert any("rendered Deployment" in p for p in got)


def test_the_registry_placeholder_is_stripped_as_k3s_stages_it():
    assert _bundled_image(_EXCERPT) == "rancher/mirrored-coredns-coredns:1.14.7"
