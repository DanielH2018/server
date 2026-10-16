"""`probe.py health <tag>` over a role's vendored files, not only its templates (H1.1d).

H1's plumbing roles apply upstream manifests byte for byte from `files/upstream/`, with
kustomize patches in `files/patches/`. Neither is a template, so a resolver that rendered only
`templates/*.j2` would find no workload in `metallb` or `longhorn` and pass their gate with
nothing checked. No role on master has vendored files yet, so each case builds a fixture role.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_vendored.py
"""

import pytest

from diagnostics.probe_lib.health_kubectl import WORKLOAD_KINDS
from diagnostics.probe_lib.health_vendored import (
    require_a_vendored_workload,
    vendored_targets,
)


def _manifest(kind, name, namespace=None):
    meta = f"  name: {name}\n" + (f"  namespace: {namespace}\n" if namespace else "")
    return f"apiVersion: v1\nkind: {kind}\nmetadata:\n{meta}"


def _role(tmp_path, upstream=None, patches=None):
    """A role directory `vend` holding `{filename: text}` under each vendored subdirectory."""
    role_dir = tmp_path / "vend"
    for sub, by_name in (("upstream", upstream), ("patches", patches)):
        (role_dir / "files" / sub).mkdir(parents=True)
        for name, text in (by_name or {}).items():
            (role_dir / "files" / sub / name).write_text(text)
    return role_dir


def test_a_vendored_workload_resolves_as_a_target(tmp_path):
    upstream = "---\n".join(
        [
            _manifest("Namespace", "vend-system"),
            _manifest("DaemonSet", "speaker", "vend-system"),
            _manifest("Deployment", "controller"),
            _manifest("ConfigMap", "config", "vend-system"),
        ]
    )
    role_dir = _role(
        tmp_path,
        upstream={"vend.yaml": upstream},
        patches={"speaker.yaml": _manifest("DaemonSet", "speaker")},
    )
    assert vendored_targets(role_dir, "homelab", WORKLOAD_KINDS) == {
        ("homelab", "Deployment", "controller"),
        ("vend-system", "DaemonSet", "speaker"),
    }
    assert vendored_targets(role_dir, "homelab", {"Deployment", "StatefulSet"}) == {
        ("homelab", "Deployment", "controller"),
    }


def test_a_vendored_role_that_resolves_no_workload_fails_health(tmp_path):
    """run_health turns this raise into exit 1, so the message must not read as a skip.

    The deploy notifier reports a tag `skipped` when the gate's line carries one of
    `NOT_APPLICABLE_MARKERS`, and a vendored role with no workload is a failed gate.
    """
    from deploy_tools.deploy_lib import detach_notify

    role_dir = _role(tmp_path, upstream={"vend.yaml": _manifest("ConfigMap", "config")})
    targets = vendored_targets(role_dir, "homelab", WORKLOAD_KINDS)
    with pytest.raises(RuntimeError, match="resolve no Deployment") as raised:
        require_a_vendored_workload(role_dir, targets)
    message = str(raised.value)
    assert not [m for m in detach_notify.NOT_APPLICABLE_MARKERS if m in message]


def test_a_role_without_vendored_files_may_resolve_no_workload(tmp_path):
    """media-volume and netpol-baseline pass health with no workload, and still do."""
    (tmp_path / "plain" / "templates").mkdir(parents=True)
    require_a_vendored_workload(tmp_path / "plain", [])


def test_a_patch_naming_an_undeclared_workload_raises(tmp_path):
    role_dir = _role(
        tmp_path,
        upstream={"vend.yaml": _manifest("DaemonSet", "speaker", "vend-system")},
        patches={"manager.yaml": _manifest("DaemonSet", "manager")},
    )
    with pytest.raises(RuntimeError, match="patches DaemonSet manager"):
        vendored_targets(role_dir, "homelab", WORKLOAD_KINDS)


def test_a_vendored_file_that_does_not_parse_raises(tmp_path):
    role_dir = _role(tmp_path, upstream={"vend.yaml": "kind: [DaemonSet\n"})
    with pytest.raises(RuntimeError, match="does not parse"):
        vendored_targets(role_dir, "homelab", WORKLOAD_KINDS)
