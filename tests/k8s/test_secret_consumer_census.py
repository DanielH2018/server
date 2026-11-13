#!/usr/bin/env python3
"""A rotation is only finished when every consumer holds the new value.

`secret_rotation.py audit` answers "what is due"; this census answers "who is still holding
the old one". A rotation that misses a consumer leaves it holding a dead credential: a missed
Sonarr or Radarr API key makes exportarr 401 on every scrape. daniel-box's fake-remux component is
a host-plane consumer, which Flux cannot reach.

`tree_consumers()` measures the answer from the tree. These tests pin the two invariants that
make it trustworthy, each with the input it must accept AND the input it must reject — a
census that finds everything and one that finds nothing look identical from the passing side.

Run: uv run pytest tests/k8s/test_secret_consumer_census.py
"""

from secrets_mgmt.consumers import consumer_commands, tree_consumers


# The incident this file exists for, kept as the accept case: each of these renders or reads
# sonarr_api_key.
SONARR_KEY_CONSUMERS = {
    "autofix-bridge": "k8s",
    "configarr": "k8s",
    "daniel-box": "host",
    "homepage": "k8s",
    "janitorr": "k8s",
    "monitor-bridge": "k8s",
    "sonarr": "k8s",
}


def test_the_census_finds_every_known_consumer_of_the_secret_that_caused_the_incident():
    assert tree_consumers("sonarr_api_key") == SONARR_KEY_CONSUMERS


def test_the_census_reports_nothing_for_a_name_no_role_references():
    """The control. Without it, a census that matched everything would pass the test above."""
    assert tree_consumers("sonarr_api_key_that_does_not_exist_anywhere") == {}


def test_the_census_credits_a_role_rendering_the_secret_out_of_another_roles_file(
    tmp_path,
):
    """uptime-kuma rendered every bridge token from monitor-bridge's check table (#3781), until
    M6 batch A6 deleted those tiles. A fixture tree keeps the mechanism pinned: the owner holds
    the name in a file, and a reader role looks that file up."""
    owner = tmp_path / "k8s/roles/owner"
    reader = tmp_path / "k8s/roles/reader/templates"
    (owner / "files").mkdir(parents=True)
    reader.mkdir(parents=True)
    (owner / "files/table.py").write_text('TOKEN = "fixture_a6_push_token"\n')
    (reader / "x.yaml.j2").write_text(
        "data: {{ lookup('file', role_file('files/table.py', role='owner')) }}\n"
    )
    assert tree_consumers("fixture_a6_push_token", str(tmp_path)) == {
        "owner": "k8s",
        "reader": "k8s",
    }


def test_the_census_names_the_pi_package_as_a_consumer_of_autoheals_webhook():
    """H5.7 moved the first consumer out of `ansible/`; a corpus walk that skips `hosts/components/`
    drops `daniel-pi` here, and the operator would never be told to run `host_secrets.py sync`."""
    consumers = tree_consumers("monitor_discord_webhook_url")

    assert consumers["daniel-pi"] == "host"
    assert {"monitor-bridge", "observability"} <= set(consumers)


def test_the_census_names_the_pi_package_as_a_consumer_of_alloys_http_password():
    """H5.8 moved the second consumer, alloy's password, out of `ansible/`; it is the member that
    says the second walk root finds a key that only a service file under the component reads."""
    consumers = tree_consumers("alloy_pi_http_password")

    assert consumers == {"daniel-pi": "host"}


def test_the_repair_commands_give_the_operator_only_sync_and_the_package_path():
    commands = consumer_commands("monitor_discord_webhook_url")

    (sync,) = [c for c in commands if "host_secrets.py sync" in c]
    assert sync.startswith(
        "`uv run python scripts/secrets_mgmt/host_secrets.py sync daniel-pi`"
    )
    assert "operator-only" in sync
    assert "`hosts/daniel-pi/package.yml`" in sync


def test_the_census_separates_the_host_plane_from_the_k8s_plane():
    consumers = tree_consumers("sonarr_api_key")

    assert consumers["daniel-box"] == "host", (
        "daniel-box's fake-remux is a host-plane consumer, which Flux cannot reach; if it "
        "stops being classified as a host package, the census stops sending the operator to "
        "`host_secrets.py sync`"
    )
    assert consumers["sonarr"] == "k8s"


def test_the_repair_commands_route_each_plane_to_the_path_that_reaches_it():
    commands = consumer_commands("sonarr_api_key")

    host_cmd = [c for c in commands if "host_secrets.py sync" in c]
    flux_cmd = [c for c in commands if "is a k8s entry, so Flux applies it" in c]

    assert len(host_cmd) == 1
    assert "sync daniel-box" in host_cmd[0]
    assert any(c.startswith("sonarr is a k8s entry") for c in flux_cmd)
    assert not any("ansible-playbook" in c for c in commands)


def test_a_role_directory_outside_the_k8s_plane_is_not_a_consumer(tmp_path):
    """`_census_corpus` walks the disk, so a leftover `roles/setup/` (a stale `__pycache__`
    survives a deleted role) must name no consumer and print no `ansible-playbook` command."""
    stale = tmp_path / "ansible/roles/setup/legacy/templates"
    stale.mkdir(parents=True)
    (stale / "x.j2").write_text("token: {{ fixture_stale_setup_token }}\n")

    assert tree_consumers("fixture_stale_setup_token", str(tmp_path)) == {}
    assert consumer_commands("fixture_stale_setup_token", repo=str(tmp_path)) == []
