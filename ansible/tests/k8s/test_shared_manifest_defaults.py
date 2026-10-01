#!/usr/bin/env python3
"""`k8s/manifests` renders a shared manifest for a role that ships none of its own (#2872).

WHY THIS EXISTS. 25 roles' `templates/service.yaml.j2` were byte-identical three-line wrappers
around the `service()` macro, and 16 roles' `templates/ingressroute.yaml.j2` were the same
shape around `ingressroute()` (#3043). They are gone, and `k8s/manifests` renders
`ansible/templates/service-default.yaml.j2` and `ansible/templates/ingressroute-default.yaml.j2`
for each of them instead. Three things have to stay true for that to keep working, and each
fails silently on its own:

* The deploy's map (`manifests_shared_defaults`, a role default) and the offline harnesses' map
  (`lib.k8s_roles.SHARED_MANIFEST_DEFAULTS`) name the same files. Drift here does not break a
  deploy — it breaks the render coverage, so the validator stops schema-checking 25 Services
  and reads green with fewer templates than yesterday.
* `service_port_name` and `service_extra_ports` on a containers_list entry still reach the
  rendered Service. They are the two variants those 25 roles carried in their own templates;
  an entry key nothing reads renders a Service missing a port name Kubernetes needs.
* The render task actually resolves the shared source. A `src` that lost the fallback fails the
  deploy loudly for those 25 roles, but only on a deploy — nothing else runs that task.

The last test is each issue's own verify-by: no two roles carry a byte-identical
`templates/service.yaml.j2`, or a byte-identical `templates/ingressroute.yaml.j2`.

Run: uv run pytest ansible/tests/k8s/test_shared_manifest_defaults.py
"""

import hashlib
from collections import defaultdict

import pytest

from lib import yaml_fast
from lib.k8s_roles import SHARED_MANIFEST_DEFAULTS, shared_default_templates
from _helpers import ANSIBLE
from _k8s_render import render_role_template
from _role_census import role_dirs

MANIFESTS = ANSIBLE / "roles" / "k8s" / "manifests"
SHARED_TPL = ANSIBLE / "templates"

# The roles whose Service the shared default renders. A census the tests below size themselves
# against, so a fallback that silently stopped resolving fails by name rather than by an
# `all(...)` over an empty set. Members, not just a count: #2872 deleted exactly these.
ROLES_WITH_A_DEFAULT_SERVICE = frozenset(
    {
        "artifacts",
        "authelia",
        "bazarr",
        "bento-pdf",
        "code-server",
        "docs",
        "headlamp",
        "healthchecks",
        "home-assistant",
        "homelab-mcp",
        "homepage",
        "ical-proxy",
        "jellyfin",
        "littlelink",
        "livesync",
        "loki-homelab",
        "navidrome",
        "peanut",
        "prowlarr",
        "radarr",
        "sonarr",
        "speedtest",
        "texbrain",
        "uptime-kuma",
        "zigbee2mqtt",
    }
)


# The roles whose IngressRoute the shared default renders — #3043's half of the census above.
# Every one of them is routed (its entry carries a `hostname`), which is also what
# `ansible/filter_plugins/toposort.py` now derives their traefik ordering edge from.
ROLES_WITH_A_DEFAULT_INGRESSROUTE = frozenset(
    {
        "bazarr",
        "bento-pdf",
        "code-server",
        "homepage",
        "jellyfin",
        "littlelink",
        "pihole",
        "prowlarr",
        "qbittorrent",
        "radarr",
        "sonarr",
        "tdarr",
        "texbrain",
        "uptime-kuma",
        "wg-easy",
        "zigbee2mqtt",
    }
)

# Each shared default's template, the census of roles taking it, and the basename the caller
# names in `manifests_files`. The tests below parametrise over this rather than carrying one
# copy per basename.
SHARED_DEFAULT_CENSUS = {
    "service.yaml": ("service-default.yaml.j2", ROLES_WITH_A_DEFAULT_SERVICE),
    "ingressroute.yaml": (
        "ingressroute-default.yaml.j2",
        ROLES_WITH_A_DEFAULT_INGRESSROUTE,
    ),
}


def _manifests_defaults() -> dict:
    return yaml_fast.safe_load((MANIFESTS / "defaults" / "main.yml").read_text()) or {}


def test_the_two_maps_name_the_same_shared_templates():
    """The deploy's `manifests_shared_defaults` and the harnesses' copy of it agree."""
    assert (
        _manifests_defaults()["manifests_shared_defaults"] == SHARED_MANIFEST_DEFAULTS
    ), (
        "manifests_shared_defaults (ansible/roles/k8s/manifests/defaults/main.yml) and "
        "lib.k8s_roles.SHARED_MANIFEST_DEFAULTS have drifted. The deploy follows the first and "
        "every offline render harness follows the second, so a mismatch drops manifests out of "
        "the validator's corpus without failing anything."
    )


def test_every_shared_default_template_exists():
    for shared in SHARED_MANIFEST_DEFAULTS.values():
        assert (SHARED_TPL / shared).is_file(), (
            f"manifests_shared_defaults names ansible/templates/{shared}, which is missing — "
            "every role relying on that fallback fails its next deploy at the render."
        )


@pytest.mark.parametrize("basename", sorted(SHARED_DEFAULT_CENSUS))
def test_the_census_of_roles_taking_a_shared_default_holds(basename):
    """Non-vacuity: the fallback resolves for exactly the roles the move deleted a template from."""
    template, census = SHARED_DEFAULT_CENSUS[basename]
    resolved = {
        d.name
        for d in role_dirs()
        if any(p.name == template for p in shared_default_templates(d.name))
    }
    assert resolved == census, (
        f"the set of roles getting a shared default {basename} moved. Added: "
        f"{sorted(resolved - census)}; lost: "
        f"{sorted(census - resolved)}. A role dropping out of this set "
        f"silently loses its {basename} from the validator's corpus; update the census here "
        "when the move is deliberate."
    )


def test_a_plain_role_renders_the_unnamed_port_form():
    """bazarr's Service is what it was before the template left the tree."""
    rendered = render_role_template("bazarr", "service-default.yaml.j2")
    assert "  name: bazarr\n" in rendered
    assert "    - port: 6767\n" in rendered
    assert "name: http" not in rendered, (
        "the default named a port bazarr's own template left unnamed — a rendered-byte change "
        "for 19 roles that needed none."
    )


def test_service_port_name_on_the_entry_names_the_port():
    """home-assistant, homelab-mcp and zigbee2mqtt need a NAMED port; the entry is how they ask."""
    rendered = render_role_template("home-assistant", "service-default.yaml.j2")
    assert "    - name: http\n" in rendered, (
        "service_port_name on the containers_list entry no longer reaches the rendered "
        f"Service — rendered:\n{rendered}"
    )


def test_service_extra_ports_on_the_entry_adds_the_sidecar_port():
    """sonarr's exportarr port, and the primary port named beside it as Kubernetes requires."""
    rendered = render_role_template("sonarr", "service-default.yaml.j2")
    assert "    - name: metrics\n      port: 9707\n" in rendered, (
        f"service_extra_ports no longer reaches the rendered Service — rendered:\n{rendered}"
    )
    assert "    - name: http\n      port: 8989\n" in rendered, (
        "the primary port is unnamed beside a named sidecar port. Kubernetes rejects a Service "
        "mixing named and unnamed ports, and it rejects it at APPLY time — rendered:\n"
        f"{rendered}"
    )


def test_the_render_task_resolves_the_shared_source():
    """The one task that reads the map. Nothing but a deploy runs it, so read its shape."""
    tasks = yaml_fast.safe_load((MANIFESTS / "tasks" / "main.yml").read_text())
    render = next(
        t for t in tasks if t.get("name", "").startswith("Render manifests for ")
    )
    src = render["ansible.builtin.template"]["src"]
    assert "manifests_shared_defaults[item]" in src, (
        "the render task's src no longer consults manifests_shared_defaults, so every role "
        f"relying on the fallback fails at the render. src is: {src!r}"
    )
    assert "manifests_own_src" in render["vars"], (
        "the render task no longer defines manifests_own_src, the role's-own-template branch "
        "of that src expression."
    )


def _own_template_digests(basename: str) -> dict[str, list[str]]:
    """Every role's own `templates/<basename>.j2`, grouped by content digest."""
    by_digest = defaultdict(list)
    for role_dir in role_dirs():
        tpl = role_dir / "templates" / f"{basename}.j2"
        if tpl.is_file():
            by_digest[hashlib.sha256(tpl.read_bytes()).hexdigest()].append(
                role_dir.name
            )
    return by_digest


@pytest.mark.parametrize("basename", sorted(SHARED_DEFAULT_CENSUS))
def test_no_two_roles_ship_a_byte_identical_template(basename):
    """Each issue's verify-by. A duplicate pair is a pair the shared default should render."""
    template, _census = SHARED_DEFAULT_CENSUS[basename]
    by_digest = _own_template_digests(basename)
    assert by_digest, f"no role ships a {basename}.j2 at all — has the tree moved?"
    duplicates = {d: roles for d, roles in by_digest.items() if len(roles) > 1}
    assert not duplicates, (
        f"these roles ship byte-identical {basename}.j2 files: "
        f"{sorted(sorted(r) for r in duplicates.values())}. Delete them and let "
        f"ansible/templates/{template} render it, moving whatever they differed on onto the "
        "containers_list entry."
    )


def test_the_duplicate_check_rejects_a_duplicate_pair():
    """Control: prove the grouping above can go red rather than passing on an empty set."""
    poisoned = defaultdict(list)
    poisoned["same"] = ["alpha", "beta"]
    duplicates = {d: roles for d, roles in poisoned.items() if len(roles) > 1}
    assert duplicates == {"same": ["alpha", "beta"]}


@pytest.mark.parametrize(
    ("basename", "role"),
    sorted(
        (basename, role)
        for basename, (_tpl, census) in SHARED_DEFAULT_CENSUS.items()
        for role in census
    ),
    ids=lambda s: s,
)
def test_a_role_taking_the_default_ships_no_template_of_its_own(basename, role):
    """The role's own template wins, so one left behind would make the fallback dead code."""
    own = ANSIBLE / "roles" / "k8s" / role / "templates" / f"{basename}.j2"
    assert not own.is_file(), (
        f"{role} ships its own {basename}.j2 again while still listed as taking the shared "
        "default. Drop it from the census in this file, or delete the template."
    )


def test_a_default_route_carries_the_authelia_middleware_for_a_gated_entry():
    """bazarr's route is what it was before the template left the tree."""
    rendered = render_role_template("bazarr", "ingressroute-default.yaml.j2")
    assert "  name: bazarr\n" in rendered
    assert "Host(`bazarr.local." in rendered, (
        f"the entry's hostname no longer reaches the Host rule — rendered:\n{rendered}"
    )
    assert "- name: authelia\n" in rendered, (
        "use_authelia on the containers_list entry no longer reaches the rendered route — "
        f"rendered:\n{rendered}"
    )


def test_a_default_route_omits_the_authelia_middleware_for_an_open_entry():
    """jellyfin is `use_authelia: false`: the reject half of the pair above."""
    rendered = render_role_template("jellyfin", "ingressroute-default.yaml.j2")
    assert "  name: jellyfin\n" in rendered
    assert "- name: authelia\n" not in rendered, (
        "the shared default gated an entry that opted out of Authelia — rendered:\n"
        f"{rendered}"
    )
