#!/usr/bin/env python3
"""Guards on the k8s manifests — the four things that fail silently.

Each of these encodes a decision whose failure mode is quiet rather than loud: nothing errors,
the deploy goes green, and the
consequence shows up later as a moved VIP, a corrupted session, an ungated service, or an
unprotected edge. A rendered-YAML check cannot catch any of them (the manifests stay valid
either way) — hence a separate suite from scripts/validate/k8s_manifests.py.

The four split along those consequences: the moved VIP is
`test_k8s_manifests_metallb.py`, the corrupted session and the ungated or unprotected edge
are `test_k8s_manifests_routes.py`, and the read-only RBAC that keeps Ansible the only write
path is `test_k8s_manifests_rbac.py`. What stays here is the build-Job leg of the
`no-mount-shadows-the-serviceaccount-token` row, plus two guards on the k8s play itself.
Pod-template hygiene the whole fleet must satisfy is `test_pod_template_hygiene.py` (priority tier) and the
`sa-less-pods-refuse-the-default-token` and `pods-disable-service-link-env-vars` rows of
`_workload_property_rows.py`. The renderer and inventory vars shared here are
`_manifest_guards.py`.

Run: uv run pytest ansible/tests/k8s/test_k8s_manifests.py
"""

from lib import yaml_fast

from _helpers import ANSIBLE
from _k8s_render import rendered_build_job_text
from _workload_property_rows import token_path_offence, volume_mounts


def test_the_build_job_mounts_nothing_over_the_serviceaccount_token():
    """The `no-mount-shadows-the-serviceaccount-token` row, over the Job no role renders alone.

    `rendered_docs()` does not carry image-builder's build Job, so the row's census cannot reach
    it. This runs the row's own selector and predicate over that Job.
    """
    job = yaml_fast.safe_load(rendered_build_job_text())
    mounts = list(volume_mounts("image-builder", "build-job.yaml.j2", job))
    assert mounts, "the build Job renders no volume mounts, so this check reads nothing"
    bad = [r for _, m in mounts if (r := token_path_offence("image-builder", m))]
    assert bad == [], bad


# The service-link env var guard is the `pods-disable-service-link-env-vars` row of
# _workload_property_rows.py. It reads the whole rendered corpus.


# The public-route and bouncer pairing guard is test_the_public_route_and_the_bouncer_move_together
# in test_crowdsec_optional.py: per host, on RENDERED output, not on template text.


#
# This one exists because the failure is invisible in exactly the wrong direction. A binding
# that grants too much does not error, does not warn, and does not change any output — the
# kubeconfig keeps working, it just quietly carries more authority than the comment above it
# claims. The whole reason this identity exists instead of copying the admin kubeconfig is
# that its ceiling is enforced, so the ceiling needs a test.


def test_no_task_reads_a_dotted_secret_key_by_jsonpath():
    """`kubectl get secret -o jsonpath={.data.users_database\\.yml}` does not error on a key
    whose name contains a dot — it prints nothing and exits 0.

    Authelia's read-back guard used that form, so it saw "no existing hash" on every run,
    regenerated the argon2 hash with a fresh salt, rewrote the Secret and rolled the pod.
    Nothing in the deploy output said so; it just never converged. Fetch {.data} and index
    the map in Ansible instead, where a missing key is visible.
    """
    for tasks in sorted((ANSIBLE / "roles" / "k8s").glob("*/tasks/main.yml")):
        for line in tasks.read_text().splitlines():
            body = line.split("#", 1)[0]
            if "jsonpath={.data." in body and "\\." in body:
                raise AssertionError(
                    f"{tasks.relative_to(ANSIBLE)}: jsonpath cannot address a dotted key — "
                    f"it returns empty and exits 0. Fetch {{.data}} and index it. Line: {line.strip()}"
                )
