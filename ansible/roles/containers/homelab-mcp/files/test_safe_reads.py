import json

import pytest

from safe_reads import (
    bearer_token_valid,
    parse_loki,
    parse_metric,
    parse_targets,
    path_is_denied,
    resolve_within_jail,
    strip_container_fields,
    summarize_container_list,
)


def test_strip_container_fields_never_leaks_env():
    inspect = {
        "Name": "/jellyfin",
        "RestartCount": 2,
        "Created": "2026-07-01T00:00:00Z",
        "State": {
            "Status": "running",
            "Running": True,
            "StartedAt": "x",
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": "jellyfin:latest",
            "Env": ["DB_PASSWORD=hunter2", "API_KEY=sk-secret"],
        },
        "Mounts": [{"Source": "/home/ubuntu/secret", "Destination": "/run/secrets"}],
    }
    out = strip_container_fields(inspect)
    blob = json.dumps(out)
    assert "hunter2" not in blob
    assert "sk-secret" not in blob
    assert "Env" not in out and "Config" not in out and "Mounts" not in out
    assert out["name"] == "jellyfin"
    assert out["image"] == "jellyfin:latest"
    assert out["running"] is True
    assert out["health"] == "healthy"
    assert out["restart_count"] == 2


def test_strip_container_fields_tolerates_missing_keys():
    assert strip_container_fields({}) == {
        "name": "",
        "image": None,
        "restart_count": None,
        "created": None,
        "health": None,
        "status": None,
        "running": None,
        "startedat": None,
        "finishedat": None,
        "exit_code": None,
    }


def test_summarize_container_list_projects_allowlist():
    items = [
        {
            "Names": ["/radarr"],
            "Image": "radarr",
            "State": "running",
            "Status": "Up 3h",
            "Labels": {"secret": "x"},
        },
    ]
    rows = summarize_container_list(items)
    assert rows == [
        {"name": "radarr", "image": "radarr", "state": "running", "status": "Up 3h"}
    ]
    assert "Labels" not in rows[0]


def test_parse_metric():
    resp = {
        "data": {
            "result": [
                {
                    "metric": {"__name__": "up", "job": "node"},
                    "value": [1700000000, "1"],
                }
            ]
        }
    }
    assert parse_metric(resp) == [
        {"metric": {"__name__": "up", "job": "node"}, "value": "1"}
    ]


def test_parse_metric_empty():
    assert parse_metric({}) == []


def test_parse_targets():
    resp = {
        "data": {
            "activeTargets": [
                {
                    "labels": {"job": "node", "instance": "node-exporter:9100"},
                    "health": "up",
                    "lastError": "",
                },
                {
                    "labels": {"job": "cadvisor", "instance": "cadvisor:8080"},
                    "health": "down",
                    "lastError": "conn refused",
                },
            ]
        }
    }
    rows = parse_targets(resp)
    assert rows[0] == {
        "job": "node",
        "instance": "node-exporter:9100",
        "health": "up",
        "last_error": "",
    }
    assert rows[1]["health"] == "down" and rows[1]["last_error"] == "conn refused"


def test_parse_loki():
    resp = {
        "data": {
            "result": [
                {
                    "stream": {"container": "traefik"},
                    "values": [["1700000000000000000", "hello"]],
                }
            ]
        }
    }
    assert parse_loki(resp) == [
        {
            "labels": {"container": "traefik"},
            "ts": "1700000000000000000",
            "line": "hello",
        }
    ]


@pytest.mark.parametrize(
    "rel",
    [
        "vars/secrets.yml",
        "vars/secrets.yaml",
        "vars/keys.txt",
        "secrets/prod.yml",
        "roles/x/files/host.key",
        "roles/x/files/tls.pem",
        "a/b/backup.age",
        "deploy.agekey",
    ],
)
def test_path_is_denied_blocks_secrets(rel):
    assert path_is_denied(rel) is True


@pytest.mark.parametrize(
    "rel",
    [
        "deploy.yml",
        "roles/containers/traefik/templates/config.yml.j2",
        "inventory/host_vars/daniel-server.yml",
        "README.md",
    ],
)
def test_path_is_denied_allows_normal_files(rel):
    assert path_is_denied(rel) is False


def test_resolve_within_jail_allows_inside(tmp_path):
    (tmp_path / "roles").mkdir()
    target = tmp_path / "roles" / "main.yml"
    target.write_text("ok")
    assert resolve_within_jail(tmp_path, "roles/main.yml") == target.resolve()


@pytest.mark.parametrize("rel", ["../etc/passwd", "../../secret", "roles/../../escape"])
def test_resolve_within_jail_rejects_traversal(tmp_path, rel):
    with pytest.raises(ValueError):
        resolve_within_jail(tmp_path, rel)


def test_resolve_within_jail_rejects_absolute(tmp_path):
    with pytest.raises(ValueError):
        resolve_within_jail(tmp_path, "/etc/passwd")


def test_resolve_within_jail_rejects_null_byte(tmp_path):
    with pytest.raises(ValueError):
        resolve_within_jail(tmp_path, "roles/\x00.yml")


def test_resolve_within_jail_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.write_text("leak")
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_within_jail(jail, "link")


def test_bearer_token_valid():
    assert bearer_token_valid("Bearer s3cret", "s3cret") is True
    assert bearer_token_valid("Bearer wrong", "s3cret") is False
    assert bearer_token_valid("s3cret", "s3cret") is False
    assert bearer_token_valid(None, "s3cret") is False
    assert bearer_token_valid("Bearer s3cret", "") is False
