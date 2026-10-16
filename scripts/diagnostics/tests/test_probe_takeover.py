"""Tests for `probe.py takeover <tag>`, the census of fields Flux's first apply strips (D5.1g).

The red half is speedtest. Its `kubectl-rollout` entry below is copied from the live read of
2026-10-16 (`kubectl -n homelab get deploy speedtest -o json --show-managed-fields`), and the
`kubectl-client-side-apply` entry is that read cut to one field of each `fieldsV1` key shape.
Values are placeholders: the census reads paths only.
"""

import argparse
import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import takeover as tk
from diagnostics.probe_lib.core import Unreadable

RESTARTED_AT = "spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"

ROLLOUT_ENTRY = {
    "apiVersion": "apps/v1",
    "fieldsType": "FieldsV1",
    "fieldsV1": {
        "f:spec": {
            "f:template": {
                "f:metadata": {
                    "f:annotations": {
                        ".": {},
                        "f:kubectl.kubernetes.io/restartedAt": {},
                    }
                }
            }
        }
    },
    "manager": "kubectl-rollout",
    "operation": "Update",
    "time": "2026-10-04T22:26:17Z",
}

CONTAINER = 'k:{"name":"speedtest"}'

CLIENT_SIDE_APPLY_ENTRY = {
    "apiVersion": "apps/v1",
    "fieldsType": "FieldsV1",
    "fieldsV1": {
        "f:metadata": {
            "f:annotations": {
                ".": {},
                "f:kubectl.kubernetes.io/last-applied-configuration": {},
            },
        },
        "f:spec": {
            "f:replicas": {},
            "f:template": {
                "f:spec": {
                    "f:containers": {
                        CONTAINER: {
                            ".": {},
                            "f:name": {},
                            "f:image": {},
                            "f:env": {
                                'k:{"name":"TZ"}': {
                                    ".": {},
                                    "f:name": {},
                                    "f:value": {},
                                }
                            },
                            "f:ports": {
                                'k:{"containerPort":80,"protocol":"TCP"}': {
                                    ".": {},
                                    "f:containerPort": {},
                                    "f:protocol": {},
                                }
                            },
                            "f:terminationMessagePath": {},
                        }
                    },
                    "f:dnsPolicy": {},
                }
            },
        },
    },
    "manager": "kubectl-client-side-apply",
    "operation": "Update",
    "time": "2026-10-04T22:20:00Z",
}

STATUS_ENTRY = {
    "apiVersion": "apps/v1",
    "fieldsType": "FieldsV1",
    "fieldsV1": {"f:status": {"f:replicas": {}}},
    "manager": "k3s",
    "operation": "Update",
    "subresource": "status",
}


def _render():
    """speedtest's Deployment as the placeholder render writes it, cut to the fixture's fields."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "speedtest", "namespace": "homelab"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "speedtest",
                            "image": "speedtest-image",
                            "env": [{"name": "TZ", "value": "zone"}],
                            "ports": [{"containerPort": 80}],
                        }
                    ]
                }
            },
        },
    }


def _live(*extra_entries):
    """The live Deployment: the render as last applied, plus the managed-field entries."""
    render = _render()
    live = copy.deepcopy(render)
    live["metadata"]["annotations"] = {tk.LAST_APPLIED: json.dumps(render)}
    live["metadata"]["managedFields"] = [
        ROLLOUT_ENTRY,
        CLIENT_SIDE_APPLY_ENTRY,
        STATUS_ENTRY,
        *extra_entries,
    ]
    return live


def _entry(manager, fields, operation="Update", subresource=None):
    entry = {"manager": manager, "operation": operation, "fieldsV1": fields}
    if subresource:
        entry["subresource"] = subresource
    return entry


def _listed(live, render):
    stripped = tk.stripped_paths(live, render)
    defaulted = tk.server_defaulted(stripped, live)
    return sorted(tk.format_path(p) for p in stripped if p not in defaulted)


def test_the_cleanup_list_is_kustomize_controllers_four_entries():
    assert tk.CLEANUP_MANAGERS == (
        ("kubectl", "Apply"),
        ("kubectl", "Update"),
        ("before-first-apply", "Update"),
        ("kustomize-controller", "Update"),
    )


def test_speedtests_rollout_restartedat_is_flagged():
    assert _listed(_live(), _render()) == [RESTARTED_AT]
    assert tk.rolls("Deployment", tk.stripped_paths(_live(), _render()))


def test_a_restartedat_the_render_declares_is_clean():
    render = _render()
    render["spec"]["template"]["metadata"] = {
        "annotations": {"kubectl.kubernetes.io/restartedAt": "${placeholder}"}
    }
    assert _listed(_live(), render) == []


def test_a_field_k3s_owns_is_clean():
    k3s = _entry("k3s", {"f:spec": {"f:progressDeadlineSeconds": {}}})
    assert _listed(_live(k3s), _render()) == [RESTARTED_AT]


def test_a_field_kubectl_owns_through_the_scale_subresource_is_clean():
    scale = _entry("kubectl", {"f:spec": {"f:paused": {}}}, subresource="scale")
    assert _listed(_live(scale), _render()) == [RESTARTED_AT]


def test_a_field_another_manager_co_owns_is_clean():
    co_owner = copy.deepcopy(ROLLOUT_ENTRY)
    co_owner["manager"] = "deploy@daniel-box"
    assert _listed(_live(co_owner), _render()) == []


def test_a_hand_patch_the_render_omits_is_flagged():
    patch = _entry("kubectl-patch", {"f:spec": {"f:paused": {}}})
    assert _listed(_live(patch), _render()) == ["spec.paused", RESTARTED_AT]


def test_before_first_apply_is_cleaned_only_as_an_update():
    fields = {"f:spec": {"f:paused": {}}}
    assert tk.is_cleaned(_entry("before-first-apply", fields))
    assert not tk.is_cleaned(_entry("before-first-apply", fields, operation="Apply"))


def test_keyed_items_and_defaulted_fields_are_clean():
    """The render declares the container, its env and its port, and the rest are defaults.

    The port's `protocol` key field is a default the render omits, so the keyed match must
    still find the item. `terminationMessagePath` and `dnsPolicy` are server defaults that
    `kubectl-client-side-apply` owns from the create and the last-applied config never held.
    """
    stripped = tk.stripped_paths(_live(), _render())
    defaulted = {tk.format_path(p) for p in tk.server_defaulted(stripped, _live())}
    assert defaulted == {
        "spec.template.spec.containers[name=speedtest].ports[containerPort=80,protocol=TCP]"
        ".protocol",
        "spec.template.spec.containers[name=speedtest].terminationMessagePath",
        "spec.template.spec.dnsPolicy",
    }


def test_without_a_last_applied_config_a_default_stays_listed():
    live = _live()
    del live["metadata"]["annotations"]
    assert "spec.template.spec.dnsPolicy" in _listed(live, _render())


def test_a_set_value_the_render_declares_is_clean():
    finalizer = 'v:"kubernetes.io/pvc-protection"'
    entry = _entry(
        "kubectl-client-side-apply", {"f:metadata": {"f:finalizers": {finalizer: {}}}}
    )
    render = {"metadata": {"finalizers": ["kubernetes.io/pvc-protection"]}}
    live = {"metadata": {"managedFields": [entry]}}
    assert tk.stripped_paths(live, render) == {}
    assert len(tk.stripped_paths(live, {"metadata": {"finalizers": []}})) == 1


def _ns(tag="speedtest"):
    return argparse.Namespace(tag=tag)


def test_run_prints_the_census_and_reports_a_forbidden_read(capsys):
    role = {
        "kind": "ClusterRole",
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "metadata": {"name": "r"},
    }
    secret = {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {"name": "s", "namespace": "homelab"},
    }

    def read(_cluster, doc):
        if doc["kind"] == "ClusterRole":
            raise tk.ReadForbidden("Error from server (Forbidden)")
        return _live()

    code = tk.run_takeover(
        _ns(), render=lambda tag, out: [_render(), role, secret], read=read
    )
    out = capsys.readouterr().out
    assert code == 0
    assert f"    {RESTARTED_AT}  (kubectl-rollout)" in out
    assert "ClusterRole r: not readable by the read-only ServiceAccount" in out
    assert "Secret homelab/s: skipped" in out


def test_a_tag_with_no_live_object_is_unreadable_not_clean():
    with pytest.raises(Unreadable):
        tk.run_takeover(
            _ns(), render=lambda tag, out: [_render()], read=lambda c, d: None
        )
