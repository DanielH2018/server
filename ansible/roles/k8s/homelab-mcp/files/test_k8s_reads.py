"""Unit tests for k8s_reads — the cluster-API tool logic (offline, no I/O)."""

import pytest

import k8s_reads


@pytest.mark.parametrize("name", ["homelab", "promtail-cmlg4", "a", "kube-system"])
def test_k8s_name_valid_accepts_names(name):
    assert k8s_reads.k8s_name_valid(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading",
        "trailing-",
        "UPPER",
        "has space",
        "a/b",
        "a?x=1",
        "a#f",
        "a..%2e",
    ],
)
def test_k8s_name_valid_rejects_url_path_chars(name):
    assert not k8s_reads.k8s_name_valid(name)


def test_parse_pod_list_counts_ready_and_restarts():
    resp = {
        "items": [
            {
                "metadata": {"name": "web-1", "namespace": "homelab"},
                "spec": {"nodeName": "daniel-box"},
                "status": {
                    "phase": "Running",
                    "startTime": "2026-08-13T10:00:00Z",
                    "containerStatuses": [
                        {"ready": True, "restartCount": 2},
                        {"ready": False, "restartCount": 1},
                    ],
                },
            }
        ]
    }
    rows = k8s_reads.parse_pod_list(resp)
    assert rows == [
        {
            "name": "web-1",
            "namespace": "homelab",
            "node": "daniel-box",
            "phase": "Running",
            "ready": "1/2",
            "restarts": 3,
            "started": "2026-08-13T10:00:00Z",
        }
    ]


def test_parse_pod_list_tolerates_missing_keys():
    assert k8s_reads.parse_pod_list({"items": [{}]}) == [
        {
            "name": None,
            "namespace": None,
            "node": None,
            "phase": None,
            "ready": "0/0",
            "restarts": 0,
            "started": None,
        }
    ]


def test_parse_workloads_merges_both_kinds():
    deploys = {
        "items": [
            {
                "metadata": {"name": "grafana", "namespace": "observability"},
                "status": {"readyReplicas": 1, "replicas": 1},
                "spec": {
                    "template": {"spec": {"containers": [{"image": "grafana:11"}]}}
                },
            }
        ]
    }
    daemonsets = {
        "items": [
            {
                "metadata": {"name": "promtail", "namespace": "homelab"},
                "status": {"numberReady": 1, "desiredNumberScheduled": 2},
                "spec": {
                    "template": {"spec": {"containers": [{"image": "promtail:3"}]}}
                },
            }
        ]
    }
    rows = k8s_reads.parse_workloads(deploys, daemonsets)
    assert [(r["kind"], r["name"], r["ready"], r["desired"]) for r in rows] == [
        ("Deployment", "grafana", 1, 1),
        ("DaemonSet", "promtail", 1, 2),
    ]
    assert rows[0]["images"] == ["grafana:11"]


def test_parse_nodes_reads_ready_and_cordon():
    resp = {
        "items": [
            {
                "metadata": {"name": "daniel-server"},
                "spec": {"unschedulable": True},
                "status": {
                    "conditions": [
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "Ready", "status": "True"},
                    ],
                    "nodeInfo": {"kubeletVersion": "v1.36.2+k3s1"},
                },
            }
        ]
    }
    assert k8s_reads.parse_nodes(resp) == [
        {
            "name": "daniel-server",
            "ready": "True",
            "schedulable": False,
            "kubelet": "v1.36.2+k3s1",
        }
    ]


def test_claude_event_rows_never_leak_the_line_body():
    # The projection is the KL1 boundary: whatever the store returns, only the
    # whitelisted metadata fields plus ts may leave. The body ("line") and any
    # non-whitelisted label are dropped, whether or not they carry content.
    parsed = [
        {
            "labels": {
                "event_name": "user_prompt",
                "prompt": "the verbatim prompt",
                "session_id": "abc",
                "detected_language": "en",
            },
            "ts": "1700000000000000000",
            "line": "user_prompt: the verbatim prompt text",
        }
    ]
    rows = k8s_reads.claude_event_rows(parsed)
    assert rows == [
        {"event_name": "user_prompt", "session_id": "abc", "ts": "1700000000000000000"}
    ]
    flat = repr(rows)
    assert "verbatim" not in flat and "line" not in flat


def test_claude_event_rows_project_the_whitelist():
    parsed = [
        {
            "labels": {
                "event_name": "tool_decision",
                "tool_name": "Bash",
                "decision": "reject",
                "success": "false",
                "model": "claude-fable-5",
            },
            "ts": "1",
            "line": "{}",
        }
    ]
    assert k8s_reads.claude_event_rows(parsed) == [
        {
            "event_name": "tool_decision",
            "tool_name": "Bash",
            "decision": "reject",
            "success": "false",
            "model": "claude-fable-5",
            "ts": "1",
        }
    ]


def test_claude_loki_base_or_raise_names_the_dark_state():
    with pytest.raises(RuntimeError, match="dark"):
        k8s_reads.claude_loki_base_or_raise("")
    assert k8s_reads.claude_loki_base_or_raise("http://x:3100") == "http://x:3100"
