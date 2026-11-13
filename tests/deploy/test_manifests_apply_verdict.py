"""Every rendered Secret document comes from a file the renderer treats as secret."""

from lib.k8s_roles import resolved_manifest_files
from _k8s_render import rendered_docs


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
    assert ("cluster-base", "readonly-rbac.yaml.j2") in secrets
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
