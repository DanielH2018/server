"""Tests for the role "At a glance" generator: the facts, the in-place writer, and the gate.

The gate is the one that matters day to day: `test_every_deployed_role_block_matches_what_the_
generator_writes_now` fails the moment a role's defaults, templates, tasks, playbook entry or
`containers_list` entry move without the block being regenerated, which is the drift #2058
(k8s) and #2096 (setup and Pi compose) were filed on. Each shape has its own red-proof pair
over a fixture role under `tmp_path`, and a non-vacuity pin naming members its census must find.
"""

from pathlib import Path

import pytest
import yaml

import gen_role_glance as g
import glance_facts as f

KNOWN_SERVICES = frozenset(
    {"sonarr", "traefik", "authelia", "home-assistant", "pihole"}
)
KNOWN_SETUP_ROLES = frozenset(
    {"gitops_deploy", "k3s", "renovate_agent", "initial_setup"}
)
KNOWN_PI_SERVICES = frozenset({"alloy", "wg-easy", "docker-proxy", "autoheal"})


# --- facts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "repository"),
    [
        ("lscr.io/linuxserver/sonarr:4.0.19.2979-ls322", "lscr.io/linuxserver/sonarr"),
        (
            "ghcr.io/gethomepage/homepage:latest@sha256:a0b71c8e",
            "ghcr.io/gethomepage/homepage",
        ),
        ("alpine", "alpine"),
        ("localhost:5000/valheim:latest", "localhost:5000/valheim"),
        ("{{ k8s_registry_pull_host }}/n8n:latest", "<k8s_registry_pull_host>/n8n"),
    ],
)
def test_image_repository_drops_the_tag_and_digest(ref, repository):
    assert g.image_repository(ref) == repository


def test_image_vars_reads_the_role_defaults_then_a_hoisted_group_var():
    defaults = {"crowdsec_k8s_lapi_port": 8080, "crowdsec_k8s_bouncer_image": "b:1"}
    group_vars = {
        "crowdsec_k8s_image": "crowdsecurity/crowdsec:v1",
        "traefik_k8s_image": "t:1",
    }
    assert g.image_vars("crowdsec", defaults, group_vars) == [
        ("crowdsec_k8s_bouncer_image", "b:1"),
        ("crowdsec_k8s_image", "crowdsecurity/crowdsec:v1"),
    ]


# --- setup-plane facts -------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


PLAYBOOK = """\
- name: Setup
  hosts: all
  roles:
    - { role: widget, tags: ["widget_tag"], when: has_widget }
    - config_files
- name: Join
  hosts: all
  tasks:
    - name: Join through the role
      ansible.builtin.include_role:
        name: widget
        tasks_from: agent
      tags: [widget_agent]
"""


def test_setup_appliers_reads_role_entries_and_include_role_tasks(tmp_path):
    _write(tmp_path / "initial_setup.yml", PLAYBOOK)
    assert f.setup_appliers("widget", tmp_path, ("initial_setup.yml",)) == [
        ("initial_setup.yml", "widget_agent", ""),
        ("initial_setup.yml", "widget_tag", "has_widget"),
    ]
    # A bare role name is applied whenever the playbook runs: no tag, no condition.
    assert f.setup_appliers("config_files", tmp_path, ("initial_setup.yml",)) == [
        ("initial_setup.yml", "", "")
    ]
    assert f.setup_appliers("absent", tmp_path, ("initial_setup.yml",)) == []


CRON_TASKS = """\
- name: Install the hourly job
  ansible.builtin.cron:
    name: hourly job
    minute: "5"
    job: /bin/true
- name: Teardown arm
  ansible.builtin.cron:
    name: gone job
    state: absent
- name: Armed only when enabled
  block:
    - name: Nested
      ansible.builtin.cron:
        name: nested job
        special_time: weekly
        state: "{{ 'present' if armed else 'absent' }}"
"""


def test_cron_jobs_descend_into_blocks_and_skip_a_literal_absent(tmp_path):
    role = tmp_path / "widget"
    _write(role / "tasks" / "main.yml", CRON_TASKS)
    assert f.cron_jobs(role) == [("hourly job", "5 * * * *"), ("nested job", "@weekly")]


def test_timer_units_reads_the_cadence_keys_in_a_fixed_order(tmp_path):
    role = tmp_path / "widget"
    _write(
        role / "templates" / "widget.timer.j2",
        "[Timer]\nOnUnitActiveSec={{ tick }}\nRandomizedDelaySec=2min\nOnBootSec=10min\n",
    )
    assert f.timer_units(role) == [
        ("widget.timer", ["OnBootSec=10min", "OnUnitActiveSec={{ tick }}"])
    ]


def test_setup_glance_lines_names_every_source_when_a_role_has_none(tmp_path):
    role = tmp_path / "widget"
    _write(role / "tasks" / "main.yml", "- name: Nothing\n  ansible.builtin.debug:\n")
    lines = f.setup_glance_lines(role, playbooks_dir=tmp_path)
    assert lines[0].startswith("- **Applied by:** no bring-up playbook entry")
    assert lines[1].startswith("- **Crons / timers:** none")


# --- Pi compose-plane facts --------------------------------------------------------------

COMPOSE = """\
services:
  proxy:
    image: lscr.io/linuxserver/socket-proxy:latest@sha256:de8215f99a9ce4bbff5d8853d7a5ae4b
    restart: unless-stopped
  proxy-lifecycle:
    image: lscr.io/linuxserver/socket-proxy:latest@sha256:de8215f99a9ce4bbff5d8853d7a5ae4b
  helper:
    image: "alpine:3.20"
"""


def test_compose_images_collapses_a_shared_image_onto_its_services(tmp_path):
    role = tmp_path / "widget"
    _write(role / "templates" / "docker-compose.yml.j2", COMPOSE)
    assert f.compose_images(role) == [
        ("lscr.io/linuxserver/socket-proxy", ["proxy", "proxy-lifecycle"]),
        ("alpine", ["helper"]),
    ]


def test_config_change_wiring_finds_the_include_role_var_or_none(tmp_path):
    wired = tmp_path / "wired"
    _write(
        wired / "tasks" / "main.yml",
        "- name: Deploy\n  ansible.builtin.include_role:\n    name: common\n"
        '  vars:\n    common_config_changed: "{{ cfg is changed }}"\n',
    )
    bare = tmp_path / "bare"
    _write(
        bare / "tasks" / "main.yml",
        "- name: Deploy\n  ansible.builtin.include_role:\n    name: common\n",
    )
    assert f.config_change_wiring(wired) == "{{ cfg is changed }}"
    assert f.config_change_wiring(bare) is None


def test_pi_glance_lines_carry_the_target_and_the_entry_facts(tmp_path):
    role = tmp_path / "widget"
    _write(role / "templates" / "docker-compose.yml.j2", COMPOSE)
    _write(role / "meta" / "deps.yml", "role_deps:\n  - docker-proxy\n")
    entry = {
        "name": "widget",
        "port": 8080,
        "networks": ["proxy"],
        "use_authelia": False,
    }
    lines = f.pi_glance_lines(entry, role)
    assert lines[0] == '- **Deploy tag:** `--tags "widget" -e target=daniel-pi`'
    assert (
        lines[2]
        == "- **Entry:** `host_vars/daniel-pi.yml` → port `8080`, networks `proxy`, no Authelia"
    )
    assert lines[3] == "- **Depends on:** `docker-proxy` (`meta/deps.yml`)"
    assert lines[4].startswith("- **Config-change wiring:** none")


# --- the in-place writer -----------------------------------------------------------------

BLOCK = g.render_block(['- **Deploy tag:** `--tags "x"`'])
DOC = (
    "# x\n\nIntro.\n\n## At a glance\n- **Why:** hand-written reasoning.\n\n## Traps\n"
)


def test_render_doc_inserts_the_block_under_the_heading_and_keeps_the_prose():
    out = g.render_doc(DOC, BLOCK)
    assert out == (
        "# x\n\nIntro.\n\n## At a glance\n" + BLOCK + "\n"
        "- **Why:** hand-written reasoning.\n\n## Traps\n"
    )


def test_render_doc_replaces_a_stale_block_and_is_idempotent():
    first = g.render_doc(DOC, g.render_block(['- **Deploy tag:** `--tags "old"`']))
    second = g.render_doc(first, BLOCK)
    assert second == g.render_doc(DOC, BLOCK)
    assert g.render_doc(second, BLOCK) == second


def test_render_doc_refuses_a_doc_without_the_heading():
    with pytest.raises(g.MissingHeading):
        g.render_doc("# x\n\n## Traps\n", BLOCK)


def test_render_doc_creates_the_heading_before_the_first_section_when_asked():
    doc = "# x\n\nIntro.\n\n## Traps\n- t\n"
    out = g.render_doc(doc, BLOCK, create_heading=True)
    assert out == "# x\n\nIntro.\n\n## At a glance\n" + BLOCK + "\n## Traps\n- t\n"
    assert g.render_doc(out, BLOCK, create_heading=True) == out


def test_render_doc_creates_the_heading_after_the_intro_when_the_doc_has_no_section():
    doc = "# x\n\nIntro line one.\nIntro line two.\n\n- a bullet\n"
    out = g.render_doc(doc, BLOCK, create_heading=True)
    assert out == (
        "# x\n\nIntro line one.\nIntro line two.\n\n## At a glance\n"
        + BLOCK
        + "\n- a bullet\n"
    )
    assert g.render_doc(out, BLOCK, create_heading=True) == out


def test_render_doc_refuses_an_unterminated_block():
    broken = "## At a glance\n" + g.BEGIN + "\n- x\n\n## Traps\n"
    with pytest.raises(ValueError, match="no `<!-- /generated_from -->`"):
        g.render_doc(broken, BLOCK)


def test_render_block_wraps_long_bullets_with_a_two_space_hang():
    block = g.render_block(["- **Auto-deploy:** " + "word " * 40])
    body = block.splitlines()[1:-1]
    assert len(body) > 1
    assert all(len(line) <= g.WIDTH for line in body)
    assert all(line.startswith("  ") for line in body[1:])


# --- the gate ----------------------------------------------------------------------------


def test_every_deployed_role_block_matches_what_the_generator_writes_now():
    """A committed block that differs from a fresh render is a hand edit or a moved source."""
    stale = g.stale_docs(write=False)
    assert stale == [], (
        f"stale At a glance block in {stale}; run `uv run python {g.SELF}` and commit"
    )


def test_the_gate_covers_the_known_services():
    # `k8s_service_entries` filters `containers_list` by platform; a filter that stopped
    # matching would make the gate above pass on nothing.
    names = {e["name"] for e in g.k8s_service_entries()}
    assert KNOWN_SERVICES <= names, sorted(KNOWN_SERVICES - names)


def test_the_gate_covers_the_known_setup_roles_and_pi_services():
    setup = {d.name for d in g.setup_role_dirs()}
    assert KNOWN_SETUP_ROLES <= setup, sorted(KNOWN_SETUP_ROLES - setup)
    assert "common" not in setup
    pi = {e["name"] for e in g.pi_service_entries()}
    assert KNOWN_PI_SERVICES <= pi, sorted(KNOWN_PI_SERVICES - pi)


def test_every_setup_and_pi_doc_carries_the_marker_under_the_heading():
    """#2096's verify-by: the heading, then the marker directly under it, on every doc."""
    docs = [d / "CLAUDE.md" for d in g.setup_role_dirs()] + [
        g.CONTAINERS_ROLES / e["name"] / "CLAUDE.md" for e in g.pi_service_entries()
    ]
    missing = []
    for doc in docs:
        lines = doc.read_text().splitlines()
        if g.HEADING not in lines or not lines[lines.index(g.HEADING) + 1].startswith(
            g.BEGIN_PREFIX
        ):
            missing.append(doc.relative_to(g.REPO).as_posix())
    assert missing == []


def _copy_role(name: str, roles: Path) -> Path:
    """`roles/<name>` with the real role's defaults, templates, tasks and doc copied in."""
    src = g.K8S_ROLES / name
    dst = roles / name
    for sub in ("defaults", "templates", "tasks"):
        if not (src / sub).is_dir():
            continue
        (dst / sub).mkdir(parents=True)
        for path in (src / sub).glob("*.y*"):
            (dst / sub / path.name).write_text(path.read_text())
    if (src / "CLAUDE.md").is_file():
        (dst / "CLAUDE.md").write_text((src / "CLAUDE.md").read_text())
    return dst


def test_a_hand_edited_block_is_flagged(tmp_path):
    """The gate's rejecting half: a copy of sonarr is clean, and one changed value makes it stale."""
    entry = next(e for e in g.k8s_service_entries() if e["name"] == "sonarr")
    dst = _copy_role("sonarr", tmp_path / "roles")
    # sonarr's claims resolve through two other roles: volume-claim's default StorageClass
    # names `sonarr-config`'s class, and media-volume declares the `media-data` it mounts.
    _copy_role("volume-claim", tmp_path / "roles")
    _copy_role("media-volume", tmp_path / "roles")
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(yaml.safe_dump({"containers_list": [entry]}))
    roles = dst.parent
    assert g.stale_k8s_docs(write=False, host_vars=host_vars, k8s_roles=roles) == []

    doc = (dst / "CLAUDE.md").read_text()
    assert '`--tags "sonarr"`' in doc
    (dst / "CLAUDE.md").write_text(
        doc.replace('`--tags "sonarr"`', '`--tags "sonar"`', 1)
    )
    assert g.stale_k8s_docs(write=False, host_vars=host_vars, k8s_roles=roles) == [
        "k8s/sonarr"
    ]


def _claims_line(entry, role_dir, roles, k3s_defaults):
    lines = g.glance_lines(
        entry, role_dir, group_vars={}, k8s_roles=roles, k3s_defaults=k3s_defaults
    )
    return next(line for line in lines if line.startswith("- **Claim"))


def test_claims_line_carries_each_claims_tier_and_moves_with_the_tier_lists(tmp_path):
    """#2105: a tier beside every named claim, read from the lists — never typed (red-proof pair)."""
    roles = tmp_path / "roles"
    entry = {"name": "svc", "platform": "k8s"}
    role = roles / "svc"
    (role / "templates").mkdir(parents=True)
    (role / "tasks").mkdir()
    # The three declaration shapes: inline PVC, a volume-claim include, a `claimName:`
    # reference to a claim another role declares off Longhorn.
    (role / "templates" / "pvc.yaml.j2").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: svc-cache\n"
        "spec:\n  storageClassName: longhorn-nobackup\n"
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "      volumes:\n"
        "        - persistentVolumeClaim:\n            claimName: svc-config\n"
        "        - persistentVolumeClaim:\n            claimName: media-data\n"
    )
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n    name: k8s/volume-claim\n"
        "  vars:\n    volume_claim_name: svc-config\n"
        "    volume_claim_storage_class: longhorn\n"
    )
    (roles / "media-volume" / "templates").mkdir(parents=True)
    (roles / "media-volume" / "templates" / "pvc.yaml.j2").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: media-data\n"
        "spec:\n  storageClassName: media-local\n"
    )
    k3s_defaults = tmp_path / "k3s.yml"
    k3s_defaults.write_text(
        yaml.safe_dump({"k3s_longhorn_weekly_volumes": ["homelab/svc-config"]})
    )
    assert _claims_line(entry, role, roles, k3s_defaults) == (
        "- **Claims:** `svc-config` (weekly -> B2 (default target)), "
        "`media-data` (not Longhorn (media-local)), "
        "`svc-cache` (no backup (StorageClass longhorn-nobackup))"
    )
    # Moving the claim between lists moves the line, so the gate catches an edited list.
    k3s_defaults.write_text(
        yaml.safe_dump({"k3s_longhorn_nobackup_volumes": ["homelab/svc-config"]})
    )
    assert "`svc-config` (no backup (listed in k3s_longhorn_nobackup_volumes))" in (
        _claims_line(entry, role, roles, k3s_defaults)
    )


def test_the_committed_blocks_name_a_tier_from_each_list():
    """Non-vacuity: a resolver that stopped matching would print `unknown` everywhere and pass."""
    # `render_block` wraps a long Claims line, so read each doc with its whitespace folded.
    sonarr = " ".join((g.K8S_ROLES / "sonarr" / "CLAUDE.md").read_text().split())
    kuma = " ".join((g.K8S_ROLES / "uptime-kuma" / "CLAUDE.md").read_text().split())
    assert "`sonarr-config` (weekly -> B2 (default target))" in sonarr
    assert "`media-data` (not Longhorn (media-local))" in sonarr
    assert (
        "`uptime-kuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes))"
        in kuma
    )


def test_a_hand_edited_setup_block_is_flagged(tmp_path):
    """The setup shape's rejecting half: a fixture role renders clean, one moved cron makes it stale."""
    playbooks = tmp_path / "ansible"
    _write(playbooks / "initial_setup.yml", PLAYBOOK)
    roles = playbooks / "roles" / "setup"
    role = roles / "widget"
    _write(role / "tasks" / "main.yml", CRON_TASKS)
    _write(role / "CLAUDE.md", "# widget\n\nIntro.\n\n## Traps\n- t\n")
    assert g.stale_setup_docs(
        write=False, setup_roles=roles, playbooks_dir=playbooks
    ) == ["setup/widget"]
    assert g.stale_setup_docs(
        write=True, setup_roles=roles, playbooks_dir=playbooks
    ) == ["setup/widget"]
    assert (
        g.stale_setup_docs(write=False, setup_roles=roles, playbooks_dir=playbooks)
        == []
    )
    doc = (role / "CLAUDE.md").read_text()
    assert "- t\n" in doc and "## Traps" in doc
    _write(
        role / "tasks" / "main.yml", CRON_TASKS.replace('minute: "5"', 'minute: "6"')
    )
    assert g.stale_setup_docs(
        write=False, setup_roles=roles, playbooks_dir=playbooks
    ) == ["setup/widget"]


def test_a_hand_edited_pi_block_is_flagged(tmp_path):
    """The Pi shape's rejecting half, over a fixture role and a one-entry host_vars."""
    roles = tmp_path / "containers"
    role = roles / "widget"
    _write(role / "templates" / "docker-compose.yml.j2", COMPOSE)
    _write(
        role / "CLAUDE.md", "# widget\n\nIntro.\n\n## At a glance\n- **Why:** kept.\n"
    )
    host_vars = _write(
        tmp_path / "daniel-pi.yml",
        yaml.safe_dump(
            {"containers_list": [{"name": "widget", "networks": ["proxy"]}]}
        ),
    )
    assert g.stale_pi_docs(
        write=True, pi_host_vars=host_vars, containers_roles=roles
    ) == ["containers/widget"]
    assert (
        g.stale_pi_docs(write=False, pi_host_vars=host_vars, containers_roles=roles)
        == []
    )
    doc = (role / "CLAUDE.md").read_text()
    assert "- **Why:** kept." in doc
    (role / "CLAUDE.md").write_text(
        doc.replace("networks `proxy`", "networks `apps`", 1)
    )
    assert g.stale_pi_docs(
        write=False, pi_host_vars=host_vars, containers_roles=roles
    ) == ["containers/widget"]
