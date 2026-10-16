"""`probe.py health <tag>` over a role's vendored files, not only its templates (H1.1d).

H1's plumbing roles apply upstream manifests byte for byte from `files/upstream/`, with
kustomize patches in `files/patches/`. Neither is a template, so a resolver that rendered only
`templates/*.j2` would find no workload in `metallb` or `longhorn` and pass their gate with
nothing checked. No role on master has vendored files yet, so each case builds a fixture role.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_vendored.py
"""

import pytest

from diagnostics.probe_lib import health
from diagnostics.probe_lib.health_kubectl import WORKLOAD_KINDS
from diagnostics.probe_lib.health_vendored import (
    require_a_vendored_workload,
    vendored_targets,
)

# The gate resolves only a role with a k8s `containers_list` entry, so a fixture role that
# goes through `health` borrows a real entry's name. It has no templates, so only its
# vendored files can resolve anything.
_FIXTURE_ROLE = "sonarr"


def _manifest(kind, name, namespace=None):
    meta = f"  name: {name}\n" + (f"  namespace: {namespace}\n" if namespace else "")
    return f"apiVersion: v1\nkind: {kind}\nmetadata:\n{meta}"


def _role(roles_dir, upstream=None, patches=None, name=_FIXTURE_ROLE):
    """A role directory with no templates and `{filename: text}` under each vendored subdir."""
    role_dir = roles_dir / name
    (role_dir / "templates").mkdir(parents=True)
    for sub, by_name in (("upstream", upstream), ("patches", patches)):
        (role_dir / "files" / sub).mkdir(parents=True)
        for filename, text in (by_name or {}).items():
            (role_dir / "files" / sub / filename).write_text(text)
    return role_dir


def test_health_resolves_a_vendored_workload_as_a_target(tmp_path):
    upstream = "---\n".join(
        [
            _manifest("Namespace", "vend-system"),
            _manifest("DaemonSet", "speaker", "vend-system"),
            _manifest("Deployment", "controller"),
            _manifest("ConfigMap", "config", "vend-system"),
        ]
    )
    _role(
        tmp_path,
        upstream={"app.yaml": upstream},
        patches={"speaker.yaml": _manifest("DaemonSet", "speaker")},
    )
    assert health.role_workload_targets(_FIXTURE_ROLE, "homelab", tmp_path) == [
        ("homelab", "Deployment", "controller"),
        ("vend-system", "DaemonSet", "speaker"),
    ]
    assert health.role_scalable_targets(_FIXTURE_ROLE, "homelab", tmp_path) == [
        ("homelab", "Deployment", "controller"),
    ]


def test_health_fails_a_vendored_role_that_resolves_no_workload(tmp_path, capsys):
    """Exit 1, and a line the deploy notifier cannot read as a skip.

    The notifier reports a tag `skipped` when the gate's line carries one of
    `NOT_APPLICABLE_MARKERS`, and a vendored role with no workload is a failed gate.
    """
    from deploy_tools.deploy_lib import detach_notify

    _role(tmp_path, upstream={"app.yaml": _manifest("ConfigMap", "config")})
    code = health.run_health(
        _FIXTURE_ROLE, cluster="prod", served="prod", roles_dir=tmp_path
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "resolve no Deployment, DaemonSet or StatefulSet" in out
    assert not [m for m in detach_notify.NOT_APPLICABLE_MARKERS if m in out], out


def test_a_role_without_vendored_files_may_resolve_no_workload(tmp_path):
    """media-volume and netpol-baseline pass health with no workload, and still do."""
    (tmp_path / "plain" / "templates").mkdir(parents=True)
    require_a_vendored_workload(tmp_path / "plain", [])


def test_a_patch_naming_an_undeclared_workload_raises(tmp_path):
    role_dir = _role(
        tmp_path,
        upstream={"app.yaml": _manifest("DaemonSet", "speaker", "vend-system")},
        patches={"manager.yaml": _manifest("DaemonSet", "manager")},
    )
    with pytest.raises(RuntimeError, match="patches DaemonSet manager"):
        vendored_targets(role_dir, "homelab", WORKLOAD_KINDS)


def test_a_vendored_file_that_does_not_parse_raises(tmp_path):
    role_dir = _role(tmp_path, upstream={"app.yaml": "kind: [DaemonSet\n"})
    with pytest.raises(RuntimeError, match="does not parse"):
        vendored_targets(role_dir, "homelab", WORKLOAD_KINDS)
