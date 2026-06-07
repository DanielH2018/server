# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
from deploy_logic import services_from_changed_paths, next_action, container_names


def test_single_service_template():
    paths = ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor"}
    assert cs.broad is False


def test_multiple_services():
    paths = [
        "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        "ansible/roles/containers/couchdb/templates/docker-compose.yml.j2",
    ]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor", "couchdb"}
    assert cs.broad is False


def test_archived_service_is_ignored():
    paths = ["ansible/roles/containers/archive/duplicati/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


def test_shared_template_is_broad():
    paths = ["ansible/templates/resources.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_host_vars_is_broad():
    paths = ["ansible/inventory/host_vars/daniel-server.yml"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_unrelated_path_ignored():
    paths = ["docs/superpowers/specs/x.md", "README.md"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


def test_next_action_noop_when_in_sync():
    assert next_action("aaa", "aaa", None) == "noop"


def test_next_action_skip_when_origin_is_hold():
    assert next_action("aaa", "bad", "bad") == "skip_hold"


def test_next_action_deploy_when_origin_ahead():
    assert next_action("aaa", "bbb", None) == "deploy"


def test_next_action_deploy_when_hold_is_stale():
    # origin advanced past the held bad SHA (operator reverted) -> deploy again
    assert next_action("aaa", "ccc", "bad") == "deploy"


# A role may run several containers; the bumped image's container is often NOT
# the role-named one (e.g. cadvisor lives in the prometheus role). The health
# gate must inspect the actual container_name values from the rendered compose.
def test_container_names_multi_container():
    compose = (
        "services:\n"
        "  influxdb:\n"
        "    container_name: scrutiny-influxdb\n"
        "  web:\n"
        "    container_name: scrutiny\n"
        "  collector:\n"
        "    container_name: scrutiny-collector\n"
    )
    assert container_names(compose) == ["scrutiny-influxdb", "scrutiny", "scrutiny-collector"]


def test_container_names_strips_quotes():
    assert container_names('    container_name: "cadvisor"\n') == ["cadvisor"]


def test_container_names_ignores_other_keys():
    compose = "    image: ghcr.io/google/cadvisor:v0.53.0\n    restart: unless-stopped\n"
    assert container_names(compose) == []


def test_container_names_dedupes():
    assert container_names("    container_name: a\n    container_name: a\n") == ["a"]


def test_container_names_empty():
    assert container_names("") == []
