"""Guard: the cluster CoreDNS image is reconciled from the manifest k3s stages.

WHY. The `coredns.yaml.skip` marker stops k3s applying its bundled CoreDNS addon, image
included, so the Deployment ran 1.14.4 through two k3s bumps that bundled 1.14.6 and 1.14.7
(#3257). `tasks/coredns.yml` closes that by reading the image out of the staged
`manifests/coredns.yaml`, which k3s rewrites on every start, and setting the Deployment to it.

WHY IT NEEDS A GUARD. The extraction is a regex inside a Jinja expression. A regex that stops
matching k3s's manifest yields an empty string, and a reordered task file can probe DNS before
the rollout it waits for. Both render, lint and deploy green.
"""

import base64

from ansible.template import Templar, trust_as_template

from _helpers import leaf_tasks, load_tasks
from lib.repo_paths import K3S_ROLE

_TASKS = K3S_ROLE / "tasks/coredns.yml"

# The image block of k3s v1.37.1+k3s1's manifests/coredns.yaml as k3s stages it, with
# `%{SYSTEM_DEFAULT_REGISTRY}%` already substituted to the empty string.
_STAGED_EXCERPT = """\
      containers:
      - name: coredns
        image: "rancher/mirrored-coredns-coredns:1.14.7"
        imagePullPolicy: IfNotPresent
"""


def _task(name: str) -> dict:
    matches = [t for t in leaf_tasks(load_tasks(_TASKS)) if t.get("name") == name]
    assert len(matches) == 1, (
        f"{_TASKS} has {len(matches)} tasks named {name!r}, want 1"
    )
    return matches[0]


def _extract(manifest: str) -> str:
    """Renders the role's own extraction expression through Ansible's templar, as the deploy does.

    Not a bare Jinja environment: plain Jinja unescapes `\\\\` inside a string literal and
    Ansible's templar does not, so a doubled-backslash regex passed here while every real
    `k3s-bringup.yml --tags coredns` run failed on it.
    """
    expr = _task("Extract the bundled CoreDNS image")["ansible.builtin.set_fact"][
        "k3s_coredns_bundled_image"
    ]
    content = base64.b64encode(manifest.encode()).decode()
    templar = Templar(
        loader=None, variables={"k3s_coredns_bundled_manifest": {"content": content}}
    )
    return str(templar.template(trust_as_template(expr))).strip()


def test_the_bundled_image_is_read_from_a_staged_manifest():
    assert _extract(_STAGED_EXCERPT) == "rancher/mirrored-coredns-coredns:1.14.7"
    # The RED case: no image line yields empty, which the assert task after it refuses.
    assert _extract("kind: Deployment\n") == ""


def test_the_image_is_set_only_on_drift_and_the_rollout_precedes_the_probe():
    names = [t.get("name") for t in leaf_tasks(load_tasks(_TASKS))]
    order = [
        "Read the CoreDNS manifest k3s staged for its own version",
        "Refuse a staged manifest that names no CoreDNS image",
        "Read the live CoreDNS image",
        "Run the CoreDNS image k3s bundles",
        "Wait for the CoreDNS rollout",
        "Verify cluster DNS resolves through the configured upstream",
    ]
    assert [n for n in names if n in order] == order
    patch = _task("Run the CoreDNS image k3s bundles")
    assert "k3s_coredns_live_image.stdout" in str(patch["when"])
    assert "k3s_coredns_bundled_image" in str(patch["when"])
