"""Guards on what counts as a changed apply in `roles/k8s/manifests`.

The shared restart fires when the render AND the apply both changed (#3115). The apply's
verdict is its own `changed_when`, read from `kubectl apply` stdout, and since #4339 it skips
`secret/` lines. A `stringData` Secret prints `configured` on every client-side apply, so
counting it made the conjunction fire on rendered bytes alone. The second guard pins the
assumption that makes skipping them safe.
"""

from lib.k8s_roles import resolved_manifest_files
from _helpers import REPO as _REPO
from _helpers import load_tasks as _tasks
from _helpers import render_expr as _render
from _k8s_render import rendered_docs


_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"


def _apply_changed(stdout: str) -> bool:
    """The apply task's own `changed_when`, rendered against one `kubectl apply` stdout."""
    (apply,) = [
        t
        for t in _tasks(_MANIFESTS)
        if str(t.get("name", "")).startswith("Apply manifests")
    ]
    expr = "{{ " + str(apply["changed_when"]) + " }}"
    return _render(expr, k8s_dry_run=False, manifests_apply={"stdout": stdout})


def test_a_stringdata_secret_alone_does_not_mark_the_apply_changed() -> None:
    """#4339: client-side apply prints `configured` for a `stringData` Secret on every run.

    Counted, it made the apply `changed` on every authelia deploy, so the render-AND-apply
    restart fell back to the render alone and a YAML-comment edit restarted the SSO portal.
    The stdout is shaped like that deploy's: authelia's three Secrets `configured`, and every
    other object in its directory `unchanged`, as their managedFields showed on 2026-10-10.
    """
    authelia = "\n".join(
        [
            "persistentvolumeclaim/authelia-config unchanged",
            "secret/authelia-config configured",
            "secret/authelia-crowdsec configured",
            "secret/authelia-redis configured",
            "service/authelia unchanged",
            "service/authelia-redis unchanged",
            "deployment.apps/authelia unchanged",
            "deployment.apps/authelia-redis unchanged",
            "middleware.traefik.io/authelia unchanged",
            "middleware.traefik.io/authelia-forwardauth unchanged",
            "middleware.traefik.io/authelia-strip-forwarded-target unchanged",
            "ingressroute.traefik.io/authelia unchanged",
            "ingressroute.traefik.io/authelia-public unchanged",
            "networkpolicy.networking.k8s.io/authelia-redis unchanged",
        ]
    )
    assert _apply_changed(authelia) is False
    # RED-proof on the other side: any non-Secret object that moved still counts.
    assert _apply_changed(authelia + "\nconfigmap/x configured") is True
    assert _apply_changed("deployment.apps/authelia created") is True


def test_every_secret_document_renders_as_a_secret_file() -> None:
    """The apply ignores `secret/` lines only because the `secret` trigger covers every Secret.

    That trigger reads `manifests_secret_render`, which renders `manifests_secret_files` alone. A
    Secret declared in an ordinary manifest file would change with no trigger left to restart
    its consumer. The list comes from `lib.k8s_roles.resolved_manifest_files`, the offline copy
    of the role's own resolution that `test_derived_manifest_files.py` keeps in step with it.
    """
    secrets = {
        (role, name)
        for role, name, doc in rendered_docs()
        if doc.get("kind") == "Secret"
    }
    # The one Secret whose name does not say so, listed by hand: if the census stops finding it,
    # it has stopped reading the rendered tree.
    assert ("uptime-kuma", "static-monitors.yaml.j2") in secrets
    stray = sorted(
        f"{role}/{name}"
        for role, name in secrets
        if name.removesuffix(".j2") not in resolved_manifest_files(role)[1]
    )
    assert not stray, (
        "These templates declare a Secret outside the role's manifests_secret_files, so a change "
        "to it restarts nothing: the apply's verdict skips `secret/` lines (#4339) and the "
        f"`secret` trigger never renders them. Name the file `secret.yaml`, `<x>-secret.yaml` or `secret-<x>.yaml`, or list it: {stray}"
    )
