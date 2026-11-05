"""A task file `role_callers` cannot load leaves a role on every service's path list (#7298).

`consumers_for` leaves out a role that must stay fleet-wide, so a role it narrows to too few
consumers hides a stale service.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_consumers_unloadable.py
"""

from diagnostics.probe_lib import releases_consumers as rc

INCLUDE = "- ansible.builtin.include_role:\n    name: k8s/helper\n"


def test_consumers_keep_a_role_whose_caller_does_not_load_wide(tmp_path):
    roles = tmp_path / "ansible" / "roles" / "k8s"
    for caller in ("sonarr", "radarr"):
        (roles / caller / "tasks").mkdir(parents=True)
        (roles / caller / "tasks" / "main.yml").write_text(INCLUDE)
    assert rc.consumers_for(["helper"], repo=tmp_path) == {
        "helper": frozenset({"sonarr", "radarr"})
    }

    (roles / "radarr" / "tasks" / "main.yml").write_text(INCLUDE + "- [unclosed\n")
    try:
        narrowed = rc.consumers_for(["helper"], repo=tmp_path)
    except Exception:
        return
    assert "helper" not in narrowed or "radarr" in narrowed["helper"], narrowed


def test_a_caller_that_does_not_load_keeps_the_role_on_every_services_paths(tmp_path):
    tasks = tmp_path / "ansible" / "roles" / "k8s" / "radarr" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(INCLUDE + "- [unclosed\n")
    consumers = rc.consumers_for(["helper"], repo=tmp_path)
    assert "helper" not in consumers
    for service in ("radarr", "jellyfin"):
        paths = rc.role_paths_for(service, ["helper"], consumers)
        assert "ansible/roles/k8s/helper/" in paths
