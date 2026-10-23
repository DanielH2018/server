"""manifest-prune-check skips a Flux-labelled object only while its Kustomization exists.

The real template is rendered and run under bash, with the same stubs as
`ansible/tests/setup/test_manifest_prune_check_list_failure.py`. Here the kubectl stub answers
each kind with the rows a test hands it, so a test can place a Flux-labelled object on the
cluster and choose which Kustomizations exist in flux-system.

The stub prints rows only when the template asks for them with DEPLOYMENT_JSONPATH, and fails
the `kubectl get` otherwise. A stub that printed fixed rows for any jsonpath would test the
parser against a row shape kubectl never sends. A typo in the label path, a dropped `flux=`
prefix or a reordered field would all stay green. Run live on 2026-10-14, this jsonpath
printed `flux-system kustomize-controller flux= ` for a Deployment with no label and no owner.
"""

from lib.proc_testing import run, write_exec
from _shell_render import render_shell_script

TEMPLATE = ("setup", "k3s", "manifest-prune-check.sh.j2")
SEP = "\x1e"
KUSTOMIZATIONS = "kustomizations.kustomize.toolkit.fluxcd.io"
DEPLOYMENT_JSONPATH = (
    r'jsonpath={range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{" flux="}'
    r'{.metadata.labels.kustomize\.toolkit\.fluxcd\.io/name}{" "}'
    r'{.metadata.ownerReferences}{"\n"}{end}'
)


def _run(tmp_path, deployments, kustomizations=("bento-pdf",)):
    """Run the rendered check once and return its exit code and its single `status=` log line.

    `deployments` is the `kubectl get deployment` output, one `ns name flux=<label> owner` row
    each; every other kind lists nothing. The staged manifests declare `deployment/declared`.
    `kustomizations` is None for a failed Kustomization read.
    """
    body = render_shell_script(*TEMPLATE)

    calls = tmp_path / "calls"
    calls.write_text("")
    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text(
        "kuma_push() { :; }\n"
        f'logger() {{ printf "%s{SEP}" "$*" >> "{calls}"; }}\n'
        "boot_grace_active() { return 1; }\n"
    )
    check_lib = tmp_path / "homelab-check-lib.sh"
    check_lib.write_text("write_check_textfile() { :; }\n")
    drift_lib = tmp_path / "setup-drift-lib.sh"
    drift_lib.write_text(
        'setup_drift_scan() { DRIFTED=""; STALE=""; DEPLOYED_NOTE=""; MANIFEST_NOTE=""; }\n'
    )
    push_env = tmp_path / "kuma-push.env"
    push_env.write_text("MANIFEST_PRUNE_PUSH_TOKEN=stubtoken\n")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    uv = write_exec(tmp_path / "uv", "#!/usr/bin/env bash\necho deployment/declared\n")
    rows = tmp_path / "deployments"
    rows.write_text("".join(f"{row}\n" for row in deployments))
    jsonpath = tmp_path / "jsonpath"
    jsonpath.write_text(DEPLOYMENT_JSONPATH)
    if kustomizations is None:
        kustomization_arm = "echo 'error: forbidden' >&2; exit 1"
    else:
        kustomization_arm = "printf '%s\\n' " + " ".join(kustomizations)
    kubectl = write_exec(
        tmp_path / "k3s",
        "#!/usr/bin/env bash\n"
        'case "$3" in\n'
        f'  {KUSTOMIZATIONS}) [[ "$5" == flux-system ]] || exit 1; {kustomization_arm} ;;\n'
        f'  deployment) [[ "$6" == "$(<"{jsonpath}")" ]] || exit 1; cat "{rows}" ;;\n'
        "esac\n",
    )

    replacements = {
        "[[ $EUID -ne 0 ]]": "[[ 0 -ne 0 ]]",
        "/usr/local/lib/kuma-push-lib.sh": str(kuma_lib),
        "/usr/local/lib/homelab-check-lib.sh": str(check_lib),
        "/usr/local/lib/setup-drift-lib.sh": str(drift_lib),
        "/etc/rancher/k3s/kuma-push.env": str(push_env),
        "/etc/rancher/k3s/manifests": str(manifests),
        "/etc/healthchecks/ping.env": str(tmp_path / "absent-ping.env"),
        "/usr/local/bin/uv run": f"{uv} run",
        'KUBECTL="k3s kubectl"': f'KUBECTL="{kubectl} kubectl"',
    }
    for literal, stub in replacements.items():
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"
        body = body.replace(literal, stub)

    result = run(["bash", str(write_exec(tmp_path / "check.sh", body))])
    logs = [
        e
        for e in calls.read_text().split(SEP)
        if e.startswith("-t manifest-prune-check status=")
    ]
    assert len(logs) == 1, logs
    return result.returncode, logs[0]


def test_a_labelled_object_whose_kustomization_exists_is_skipped(tmp_path):
    code, log = _run(tmp_path, ["homelab bento-pdf flux=bento-pdf "])
    assert code == 0
    assert "status=up " in log


def test_a_labelled_object_whose_kustomization_is_gone_is_flagged(tmp_path):
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Notable
    # After a handback the object keeps its label and the Kustomization is deleted.
    code, log = _run(tmp_path, ["homelab bento-pdf flux=bento-pdf "], kustomizations=())
    assert code == 1
    assert "status=down " in log
    assert "deployment/homelab/bento-pdf" in log


def test_an_unlabelled_orphan_is_flagged(tmp_path):
    code, log = _run(tmp_path, ["homelab declared flux= ", "homelab retired flux= "])
    assert code == 1
    assert "deployment/homelab/retired" in log
    assert "deployment/homelab/declared" not in log


def test_an_unlabelled_declared_object_is_clean(tmp_path):
    code, log = _run(tmp_path, ["homelab declared flux= "])
    assert code == 0
    assert "status=up " in log


def test_a_failed_kustomization_read_skips_nothing(tmp_path):
    # Fail closed: without the list the check cannot tell Flux's objects from a handback's
    # leftovers, so it checks them all and names the read failure.
    code, log = _run(
        tmp_path, ["homelab bento-pdf flux=bento-pdf "], kustomizations=None
    )
    assert code == 1
    assert "kubectl get kustomizations -n flux-system failed" in log
    assert "deployment/homelab/bento-pdf" in log


def test_an_owner_reference_after_an_empty_label_still_skips(tmp_path):
    # The label field sits between the name and the ownerReferences JSON. An empty label must
    # not shift the JSON into it and leave the owner unread.
    code, log = _run(tmp_path, ['homelab child flux= [{"kind":"ReplicaSet"}]'])
    assert code == 0
    assert "status=up " in log
