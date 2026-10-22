"""`takeover_census.py` over an object whose dry-run apply declares no field (#5902).

Upstream MetalLB ships Secret `metallb-system/metallb-webhook-cert` with metadata only, for its
controller to fill at runtime. A server-side apply of it declares no field, so the dry run
records no kustomize-controller Apply entry. The census reports such an object in its own
category, naming the managers that own its live fields, and goes on to the next object. It
exits non-zero only when a cleaned manager owns a live field the render does not declare.

Each input pairs that Secret with a Deployment. The Secret's key sorts first (core group ""
before "apps"), so a census that stopped at it would never reach the Deployment.
"""

import copy
import json

import takeover_census as tc

RESTARTED_AT = "spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"
SECRET = "Secret metallb-system/metallb-webhook-cert"
DEPLOYMENT = "Deployment homelab/speedtest"

REPLICAS = {"f:spec": {"f:replicas": {}}}
RESTART = {
    "f:spec": {
        "f:template": {
            "f:metadata": {"f:annotations": {"f:kubectl.kubernetes.io/restartedAt": {}}}
        }
    }
}
CERT_DATA = {"f:data": {"f:ca.crt": {}, "f:tls.crt": {}, "f:tls.key": {}}}
LAST_APPLIED = {
    "f:metadata": {
        "f:annotations": {"f:kubectl.kubernetes.io/last-applied-configuration": {}}
    }
}


def entry(manager, operation, fields):
    return {"manager": manager, "operation": operation, "fieldsV1": fields}


def webhook_secret(*entries):
    """The live Secret: metadata from the vendored manifest, data from the controller."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "metallb-webhook-cert",
            "namespace": "metallb-system",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
            "managedFields": list(entries),
        },
        "data": {"ca.crt": "x", "tls.crt": "x", "tls.key": "x"},
    }


def deployment(*entries):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "speedtest",
            "namespace": "homelab",
            "managedFields": list(entries),
        },
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "x"}}
            },
        },
    }


def dry_run_declaring_nothing(live):
    """The dry run of a metadata-only object: the live body and entries, no Apply entry."""
    return copy.deepcopy(live)


def dry_run(live, declared):
    out = copy.deepcopy(live)
    out["metadata"]["managedFields"].append(
        entry("kustomize-controller", "Apply", declared)
    )
    return out


def run_main(tmp_path, capsys, secret, declared=REPLICAS):
    """`main` over the Secret and a Deployment; the render declares `declared` of it.

    The default omits restartedAt, so the Deployment's census lists it as removed and rolls.
    """
    workload = deployment(
        entry("kubectl-client-side-apply", "Update", REPLICAS),
        entry("kubectl-rollout", "Update", RESTART),
    )
    live = tmp_path / "live.json"
    dry = tmp_path / "dry.json"
    live.write_text(json.dumps({"kind": "List", "items": [secret, workload]}))
    dry.write_text(
        json.dumps(
            {
                "kind": "List",
                "items": [
                    dry_run_declaring_nothing(secret),
                    dry_run(workload, declared),
                ],
            }
        )
    )
    code = tc.main([str(live), str(dry)])
    return code, capsys.readouterr().out


def block(text, heading):
    """The lines of the object block that opens with `heading`, up to the next object."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith(" "):
            break
        out.append(line)
    return out


def test_a_metadata_only_secret_is_reported_and_the_objects_after_it_are_censused(
    tmp_path, capsys
):
    """Verify-by: a metadata-only Secret is reported, and the Deployment after it measured."""
    secret = webhook_secret(entry("metallb-controller", "Update", CERT_DATA))
    _, out = run_main(tmp_path, capsys, secret)
    assert any(line.startswith(SECRET) for line in out.splitlines()), out
    assert f"{DEPLOYMENT}: rolls" in out.splitlines(), out
    assert any(
        line.strip() == f"{RESTARTED_AT}  (kubectl-rollout)"
        for line in block(out, DEPLOYMENT)
    ), out


def test_a_declares_nothing_object_lists_the_managers_that_own_its_live_fields(
    tmp_path, capsys
):
    secret = webhook_secret(
        entry("kubectl-client-side-apply", "Update", LAST_APPLIED),
        entry("metallb-controller", "Update", CERT_DATA),
    )
    _, out = run_main(tmp_path, capsys, secret)
    assert any(line.startswith(SECRET) for line in out.splitlines()), out
    secret_block = "\n".join(block(out, SECRET))
    assert "metallb-controller" in secret_block
    assert "kubectl-client-side-apply" in secret_block


def test_a_declares_nothing_object_says_whether_a_cleaned_manager_owns_a_field(
    tmp_path, capsys
):
    """The same Secret, owned once by `kubectl` (cleaned) and once by an outside manager.

    With the manager names masked, the two blocks still differ: the census marks the cleaned
    one. Masking keeps the assertion off the wording the fix chooses.
    """
    blocks = []
    for manager in ("kubectl", "metallb-controller"):
        secret = webhook_secret(entry(manager, "Update", CERT_DATA))
        _, out = run_main(tmp_path, capsys, secret)
        assert any(line.startswith(SECRET) for line in out.splitlines()), out
        blocks.append("\n".join(block(out, SECRET)).replace(manager, "<manager>"))
    assert blocks[0] != blocks[1], blocks[0]


def test_a_declares_nothing_object_no_cleaned_manager_owns_exits_zero(tmp_path, capsys):
    """The render declares every Deployment field, so only the Secret could fail the exit."""
    secret = webhook_secret(entry("metallb-controller", "Update", CERT_DATA))
    everything = {"f:spec": {**REPLICAS["f:spec"], **RESTART["f:spec"]}}
    code, out = run_main(tmp_path, capsys, secret, declared=everything)
    assert code == 0, out


def test_a_cleaned_manager_owning_an_undeclared_field_exits_non_zero_after_the_census(
    tmp_path, capsys
):
    """The failing exit still follows a full census: the Deployment after it is printed.

    `kubectl` owns the cert data, a field the flip deletes; last-applied alone would not fail
    the exit (#5933).
    """
    secret = webhook_secret(
        entry("kubectl-client-side-apply", "Update", LAST_APPLIED),
        entry("kubectl", "Update", CERT_DATA),
    )
    code, out = run_main(tmp_path, capsys, secret)
    assert f"{DEPLOYMENT}: rolls" in out.splitlines(), out
    assert code != 0


EVERYTHING = {"f:spec": {**REPLICAS["f:spec"], **RESTART["f:spec"]}}
CERT_PATHS = {"data.ca.crt", "data.tls.crt", "data.tls.key"}


def test_a_cleaned_manager_owning_data_fields_exits_non_zero_and_lists_them(
    tmp_path, capsys
):
    """`kubectl` owns the cert data: the flip deletes all three, and the exit says so."""
    secret = webhook_secret(entry("kubectl", "Update", CERT_DATA))
    code, out = run_main(tmp_path, capsys, secret, declared=EVERYTHING)
    assert code != 0, out
    assert CERT_PATHS <= {line.strip() for line in block(out, SECRET)}, out


def test_a_path_a_foreign_manager_co_owns_is_not_deleted_and_does_not_fail(
    tmp_path, capsys
):
    """`metallb-controller` also owns the data, so the flip keeps it, as `compare` rules."""
    secret = webhook_secret(
        entry("kubectl", "Update", CERT_DATA),
        entry("metallb-controller", "Update", CERT_DATA),
    )
    code, out = run_main(tmp_path, capsys, secret, declared=EVERYTHING)
    assert code == 0, out
    assert not CERT_PATHS & {line.strip() for line in block(out, SECRET)}, out


def metadata_only_namespace(*entries):
    """A vendored Namespace with metadata only, applied once by client-side `kubectl apply`."""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "longhorn-system",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
            "managedFields": list(entries),
        },
    }


def test_a_metadata_only_namespace_whose_only_cleaned_field_is_last_applied_exits_zero(
    tmp_path, capsys
):
    """Verify-by (#5933): the expected last-applied entry alone does not fail the census."""
    namespace = metadata_only_namespace(
        entry("kubectl-client-side-apply", "Update", LAST_APPLIED)
    )
    code, out = run_main(tmp_path, capsys, namespace, declared=EVERYTHING)
    assert any(
        line.startswith("Namespace longhorn-system") for line in out.splitlines()
    )
    assert code == 0, out


def test_the_metallb_secret_whose_only_cleaned_field_is_last_applied_exits_zero(
    tmp_path, capsys
):
    """The motivating MetalLB Secret: last-applied is cleaned, the cert data is foreign-owned."""
    secret = webhook_secret(
        entry("kubectl-client-side-apply", "Update", LAST_APPLIED),
        entry("metallb-controller", "Update", CERT_DATA),
    )
    code, out = run_main(tmp_path, capsys, secret, declared=EVERYTHING)
    assert any(line.startswith(SECRET) for line in out.splitlines()), out
    assert code == 0, out
