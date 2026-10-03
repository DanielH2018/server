"""A guard under a SCANNED test directory asserts on a RENDER, never on a template's text.

#2809 settled this as prose — "no `ansible/tests/services` test asserts a literal copied from
a template" — which every session re-derives by scanning the directory, and which a new guard
written against a template's bytes passes. This module is that sentence as a check: no module
in a directory `SCANNED` names reads a path under a `templates/` directory, except an entry in
`TEMPLATE_SOURCE_READERS` carrying the reason the source text is the right thing to read.

`SCANNED` grows one directory at a time, and a directory joins it only once its own readers
are converted (#3107). `services/` joined with six permanent entries; `longhorn/` joined with
none, its one reader — the daily RecurringJob's group — now read out of `_setup_render`, and
`repo/` — the directory this module itself sits in, so a new guard written here is held to the
rule — joined with one, which keys on a variable NAME. `deploy/` joined with two (#3204):
the raw-byte hash the render stamp compares, and the autodeploy derivation's source fallback
for synthetic role trees no render reaches. `k8s/` joined with two (#3203): whether a
template CALLS the shared *arr macro or copies its body, and whether two roles ship
byte-identical templates — both render differently from what they test. `staging/` joined
with none (#3205). `setup/` joined with two, each keyed on something no render carries: a
Jinja FILTER, and the behaviour of a variable no render leaves undefined (#3202). Every
test directory is now scanned. Entries are keyed
`<directory>/<module>.py`, because a basename alone would let a module in one directory
inherit another's exemption.

The exposure is the same one the `tests-read-shell-templates-rendered` row of
`scripts/tests/test_census_rows_test_renders.py` covers for `*.sh.j2` repo-wide: a value that
moves into a role default leaves the assertion matching `{{ ... }}`, and a pattern that
matches nothing passes (#3178). The sanctioned readers are `_k8s_render` (k8s manifests and
`Dockerfile*.j2`), `_setup_render` (setup-plane templates), `_compose_render` (the Pi's Compose
and config templates) and `_shell_render` (`*.sh.j2`).

Two things the rule deliberately does not reach:

* **A role's `files/` copy.** `uptime-kuma/files/*.liquid` ships verbatim, so there is no
  render to read and the text IS the artifact. `test_kuma_email_template.py` and
  `test_kuma_discord_template.py` read one each, and neither names a `templates/` path.
#3196 named one blind spot, `test_healthchecks_pings.py`, whose `ANSIBLE.rglob("*")` census
put no template path at its read site. #3190 converted that census to a render in the same
wave, and the control read it kept IS visible here — so the module is a listed reader rather
than a declared gap, and no blind-spot map is needed.

Three more spellings hid a read until #3209, #3210 and #3211, and `_render_gate.py` resolves
all three: a glob wrapped in `sorted()`, a path handed to a helper whose parameter does the
reading, and a path imported from a sibling module. Eight modules were reading a template's
source behind them — three are converted, and five joined the list below. That is the one way
the list GROWS: a detector that learns to see a read adds the entries it was blind to, where a
conversion never adds one.

Four more hid one until #3219, all of them ways the glob and the read sit at different sites: a
glob returned by a function, a glob inside a comprehension the function returns, a `+` of two
globs, and a `@pytest.mark.parametrize` whose values are the glob. Seven modules were reading
source behind them, every one of them listed below — two because a render erases the macro call
they are about, four because the file set they read is wider than any one render covers (#3223,
#3224). The fifth, the checksum-annotation census, now reads its template half out of
`_k8s_render` and keeps a source fallback for the templates no render covers (#3225).

`ansible/tests/repo/_render_gate.py` holds the detector itself — which spellings of a read it
resolves, and the one it deliberately does not treat as a read (a glob that only yields
FILENAMES).

Run: uv run pytest ansible/tests/repo/test_guard_tests_read_renders_not_templates.py
"""

import functools
from pathlib import Path

from lib.repo_paths import ANSIBLE

from _render_gate import sibling_sources, template_source_reads

TESTS = ANSIBLE / "tests"
# Every directory the rule covers. Adding one means converting its readers first.
SCANNED = (
    TESTS / "services",
    TESTS / "longhorn",
    TESTS / "repo",
    TESTS / "deploy",
    TESTS / "k8s",
    TESTS / "staging",
    TESTS / "setup",
)

# Modules that read a template's SOURCE, each with why a render cannot answer the question.
# Every entry is permanent: a render erases the thing it reads.
TEMPLATE_SOURCE_READERS = {
    "services/_kuma_entities.py": (
        "the `*_push_token` NAMES are a render INPUT — they decide which gated tiles render "
        "at all, so they are read before the render rather than out of it"
    ),
    "services/test_n8n_build_is_pinned.py": (
        "Renovate's `matchString` runs against `Dockerfile.j2` on disk, braces intact; a "
        "render is not what Renovate sees"
    ),
    "services/test_nut_fsd_login_confined.py": (
        "which ROLES name `nut_monitor_password` — every render of that secret is the same "
        "`STUB`, so the census has to key on the variable name"
    ),
    "services/test_smtp_wiring.py": (
        "a `# DECIDED:` comment in authelia's config-secret template, which no render carries"
    ),
    "services/test_strangler_bridge.py": (
        "the ABSENCE of a retired variable name (`bridge_hostname`) across every template, "
        "which a render cannot show — an undefined name renders as nothing"
    ),
    "repo/test_secret_rendering_host_scripts_have_no_log.py": (
        "which VARIABLE NAMES a `*.sh.j2` references, to decide whether its template task "
        "needs `no_log`; a render replaces every secret with the same `STUB` and drops the "
        "name that identified it"
    ),
    "services/test_healthchecks_pings.py": (
        "the control that keeps its render honest asserts a template CARRIES `{{`, which is "
        "the one claim a render erases; the census itself renders (#3190)"
    ),
    "deploy/_autodeploy.py": (
        "the fallback for a role no render reaches: the synthetic `widget_role` trees under "
        "`tmp_path` the derivation's unit tests build, and the caller-rendered roles "
        "`_k8s_render` skips, all denylisted. Every role the render reaches reads its render, "
        "and `test_the_render_reaches_every_auto_deployable_role` holds the denylisted half"
    ),
    "deploy/test_setup_render_manifest.py": (
        "hashes the template's raw BYTES — a trailing newline and a truncated-read "
        "comparison, neither of which survives a render"
    ),
    "k8s/test_arr_deployments_share_one_macro.py": (
        "whether radarr's and sonarr's templates CALL the shared macro or write its body out "
        "beside the call; both render the same Deployment, so only the source tells them "
        "apart. The macro's body itself is checked on the render"
    ),
    "k8s/test_shared_manifest_defaults.py": (
        "whether two roles ship BYTE-IDENTICAL templates the shared default should replace; "
        "each renders with its own `container_item`, so two identical sources render "
        "differently and the duplication is visible only in the bytes"
    ),
    "deploy/_autodeploy_claims.py": (
        "an UNRESOLVED claim token — `claimName: {{ inst.claim }}`, a loop variable no "
        "single-variable resolver reaches — which the caller treats as a violation. A render "
        "resolves the token or drops it, so neither outcome is distinguishable from a role "
        "with no claim at all. Reads the same synthetic role trees under `tmp_path` as "
        "`_autodeploy.py`, whose derivation it feeds"
    ),
    "k8s/test_configmap_keys_not_absorbed.py": (
        "the SOURCE key names written at the `data:` indent, which are one side of the "
        "comparison: absorption shows up as a key present in the source and absent from the "
        "PARSED render, so reading only the render erases the defect"
    ),
    "k8s/test_container_security_context_uses_the_macro.py": (
        "whether a container block CALLS `hardened_security_context()` or writes its body out; "
        "a copy that still matches renders identically to the macro call, so only the source "
        "tells them apart. The rendered result is checked by "
        "`test_container_security_context.py`"
    ),
    "k8s/test_empty_rollout_still_restarts_on_secret_change.py": (
        "the fallback for a role no render reaches: the synthetic `widget` roles under "
        "`tmp_path` the red proofs build, which have no `containers_list` entry. Every real "
        "role reads its render, and `test_the_render_reaches_every_role_rendering_a_secret` "
        "holds that nothing in the corpus falls back"
    ),
    "longhorn/test_longhorn_reap_orphan_never_scheduled.py": (
        "the fallback for a unit template no render reaches: the synthetic `.service.j2` and "
        "`.timer.j2` under `tmp_path` the red proofs write. Every real setup unit reads its "
        "render, and `test_the_render_reaches_every_setup_unit_template` holds that"
    ),
    "k8s/test_root_security_context_is_written_out_in_full.py": (
        "whether a template builds a root securityContext through "
        "`hardened_security_context(run_as_user=0)` rather than writing the block out; the "
        "render expands the macro, so the call and a hand-written copy render identically and "
        "only the source tells them apart"
    ),
    "k8s/test_workload_shell_uses_the_macros.py": (
        "whether a workload document CALLS `pod_shell()`/`spec_shell()` or copies the fields "
        "the macros emit; both render to the same YAML, so the distinction exists only in the "
        "source"
    ),
    "k8s/test_root_needs_dac_capability.py": (
        "the fallback for the three templates no render reaches: `k8s/image-builder`'s build "
        "Job and context ConfigMap and `k8s/volume-claim`'s PVC, all three `include_role` "
        "helpers whose variables arrive on the caller's include task. The census itself spans "
        "all three planes off five renders, and "
        "`test_the_render_reaches_every_role_template` holds that nothing else falls back"
    ),
    "k8s/test_volume_names_descriptive.py": (
        "the fallback for the templates `_k8s_render` skips: every role it reaches is "
        "checked from its render, and the skipped ones from source, because image-builder's "
        "build Job declares volumes no render reaches. "
        "`test_the_source_fallback_is_only_the_templates_the_render_skips` holds the "
        "volume-bearing members of that fallback (#3224)"
    ),
    "k8s/test_checksum_annotations_documented.py": (
        "the `*.yaml`/`*.yml` task files, which no render covers, plus the fallback for the "
        "four kinds of template `_k8s_render` skips — an unrendered role, a `*.sh.j2`, a "
        "`Dockerfile*` and a nested `templates/config/*.j2`. Every manifest template reads its "
        "render, and `test_every_source_fallback_is_a_template_no_render_covers` holds that "
        "nothing else falls back"
    ),
    "setup/test_registry_selftest_single_node.py": (
        "whether the agent-Job gate carries `| default([])`, which decides what happens on a "
        "host whose inventory does not define `k3s_agent_node_ips`. Every render context "
        "defines it, so no render can show the undefined case the filter covers"
    ),
    "setup/test_nut_host_secondary.py": (
        "whether the two push-token expressions carry `| mandatory`, which decides whether an "
        "undefined token fails the play or renders empty. A render resolves every secret to "
        "`STUB`, so no render distinguishes `mandatory` from `default('')`"
    ),
    "setup/test_setup_cross_role_files.py": (
        "which roles' TASK files name another role's file by path, the edge the GitOps "
        "deployer routes on. It reads no template; a task file's `src:` and `import_tasks:` "
        "paths are the subject, and the `{{ item }}` in one is what it expands"
    ),
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the directory is clean.
KNOWN_MEMBERS = frozenset(
    {
        "services/_homepage_config.py",
        "services/_jellyfin_plugins.py",
        "services/_kuma_entities.py",
        "services/test_authelia_access_tiers.py",
        "services/test_healthchecks_pings.py",
        "services/test_kuma_static_monitors.py",
        "services/test_monitor_bridge_modules.py",
        "services/test_smtp_wiring.py",
        "services/test_strangler_bridge.py",
        "longhorn/_restore_drill.py",
        "longhorn/test_daily_group_membership_is_the_r2_list.py",
        "longhorn/test_longhorn_reap_orphan_never_scheduled.py",
        "longhorn/test_longhorn_restore_drill_byte_floor.py",
        "longhorn/test_prune_backups.py",
        "repo/_render_gate.py",
        "repo/test_census_rows_roles.py",
        "repo/test_render_gate.py",
        "repo/test_guard_tests_read_renders_not_templates.py",
        "repo/test_secret_rendering_host_scripts_have_no_log.py",
        "repo/test_testpaths_covers_every_test_file.py",
        "deploy/_autodeploy.py",
        "deploy/_autodeploy_claims.py",
        "deploy/test_gitops_manual_trigger.py",
        "deploy/test_k8s_autodeploy_rollout_gates.py",
        "deploy/test_setup_render_manifest.py",
        "k8s/_manifest_guards.py",
        "k8s/test_arr_deployments_share_one_macro.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "k8s/test_container_security_context_uses_the_macro.py",
        "k8s/test_empty_rollout_still_restarts_on_secret_change.py",
        "k8s/test_checksum_annotations_documented.py",
        "k8s/test_root_security_context_is_written_out_in_full.py",
        "k8s/test_k8s_manifests.py",
        "k8s/test_root_needs_dac_capability.py",
        "k8s/test_volume_names_descriptive.py",
        "k8s/test_workload_shell_uses_the_macros.py",
        "k8s/test_shared_manifest_defaults.py",
        "k8s/test_tls_cert_resolver_optional.py",
        "k8s/test_vip_pins.py",
        "staging/_fence_probe.py",
        "staging/test_etcd_drill_vm.py",
        "staging/test_staging_egress_fence.py",
        "staging/test_staging_network.py",
        "setup/_kuma_monitors.py",
        "setup/test_claude_code_on_daniel_server.py",
        "setup/test_claude_memory_sync.py",
        "setup/test_claude_rc_unit.py",
        "setup/test_coredns_metrics_binds_wildcard.py",
        "setup/test_etcd_metrics_single_switch.py",
        "setup/test_k3s_control_plane_hardening.py",
        "setup/test_loki_route_witness.py",
        "setup/test_nut_host_secondary.py",
        "setup/test_registry_selftest_single_node.py",
    }
)


def _modules() -> dict[str, Path]:
    """Every scanned module, by its `<directory>/<module>.py` key."""
    return {
        f"{directory.name}/{p.name}": p
        for directory in SCANNED
        for p in sorted(directory.glob("*.py"))
        if p.name != "__init__.py"
    }


@functools.cache
def _importable(directory: str) -> tuple[tuple[str, str], ...]:
    """Every module a test in `directory` can import, as (module name, text) pairs.

    Cached as a tuple because `functools.cache` needs a hashable return value and the census
    asks for the same directory once per module in it.
    """
    return tuple(sibling_sources(TESTS / directory, TESTS).items())


def test_the_census_reaches_every_known_module():
    missing = KNOWN_MEMBERS - set(_modules())
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_listed_module_still_exists():
    """A stale entry lets a whole module back out of the rule."""
    found = _modules()
    gone = sorted(name for name in TEMPLATE_SOURCE_READERS if name not in found)
    assert gone == [], f"listed modules that no longer exist: {gone}"


def offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    """The modules in `sources` (name -> text) that read a template and are not exempt.

    Takes the sources rather than reading the directory, so the red proof below can hand it a
    module that does not exist on disk: a gate only ever observed passing is a gate with no
    evidence it can fail.
    """
    by_directory: dict[str, dict[str, str]] = {}
    for name, text in sources.items():
        by_directory.setdefault(name.split("/")[0], {})[Path(name).stem] = text
    return {
        name: reads
        for name, text in sorted(sources.items())
        if name not in TEMPLATE_SOURCE_READERS
        and (
            reads := template_source_reads(
                text,
                dict(_importable(directory := name.split("/")[0]))
                | by_directory[directory],
            )
        )
    }


def test_no_services_test_reads_a_templates_source():
    found = offenders({key: p.read_text() for key, p in _modules().items()})
    assert found == {}, (
        "these tests assert on a template's SOURCE, so a value moved into a role default "
        "leaves the assertion matching `{{ ... }}`. Render it instead — `_k8s_render` for a "
        "manifest or a `Dockerfile*.j2`, `_compose_render` for the Pi's templates, "
        "`_shell_render` for a `*.sh.j2` — or add an entry to TEMPLATE_SOURCE_READERS saying "
        f"why the source text is the subject: {found}"
    )


def test_every_exempt_module_is_still_flagged():
    """Non-vacuity: a renamed exemption must fail here rather than pass by matching nothing."""
    found = _modules()
    unflagged = sorted(
        name
        for name in TEMPLATE_SOURCE_READERS
        if not template_source_reads(
            found[name].read_text(),
            dict(_importable(name.split("/")[0])),
        )
    )
    assert unflagged == [], (
        "these modules no longer read a template's source — drop them from "
        f"TEMPLATE_SOURCE_READERS: {unflagged}"
    )


def test_a_new_module_reading_a_role_template_is_named_as_an_offender():
    """The verify-by of #3196, run against a module that is not on disk."""
    found = offenders(
        {
            "services/test_new_guard.py": 'T = K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2"\n'
            'assert "npm" in T.read_text()\n',
            "services/test_already_clean.py": "from _k8s_render import rendered_texts\n"
            'assert "npm" in rendered_texts("n8n")["Dockerfile.j2"]\n',
        }
    )
    assert list(found) == ["services/test_new_guard.py"], found


def test_a_new_setup_guard_reading_a_role_template_is_named_as_an_offender():
    """The directory added in #3202, and the reason the keys carry one.

    `setup/test_nut_host_secondary.py` is exempt, so a basename-keyed exemption would wave
    through a `services/` module of the same name. Both halves are modules that are not on
    disk, which is what makes this a proof the gate can fail.
    """
    source = (
        'T = SETUP_ROLES / "claude_code" / "templates" / "claude-rc.service.j2"\n'
        'assert "Slice=" in T.read_text()\n'
    )
    found = offenders(
        {
            "setup/test_new_guard.py": source,
            "services/test_nut_host_secondary.py": source,
            "setup/test_already_clean.py": "from _setup_render import rendered_setup_text\n"
            'assert "Slice=" in rendered_setup_text("claude_code", "claude-rc.service.j2")\n',
        }
    )
    assert sorted(found) == [
        "services/test_nut_host_secondary.py",
        "setup/test_new_guard.py",
    ], found


def test_a_new_longhorn_guard_reading_a_role_template_is_named_as_an_offender():
    """The directory added in #3107, and the reason the keys carry one.

    `services/test_smtp_wiring.py` is exempt; a `longhorn/` module of the same basename is
    not, so a basename-keyed exemption would have waved this read through.
    """
    source = (
        'T = SETUP_ROLES / "k3s" / "templates" / "longhorn-recurringjob.yaml.j2"\n'
        'assert "default" in T.read_text()\n'
    )
    found = offenders(
        {
            "longhorn/test_new_guard.py": source,
            "longhorn/test_smtp_wiring.py": source,
        }
    )
    assert sorted(found) == [
        "longhorn/test_new_guard.py",
        "longhorn/test_smtp_wiring.py",
    ], found


def test_a_reverted_deploy_guard_is_named_as_an_offender():
    """The directory added in #3204, against the spelling its manual-trigger guard used to take.

    `test_gitops_manual_trigger.py` read the polkit rule's source until #3204; putting that
    read back must fail the census rather than pass under a directory it does not scan.
    """
    found = offenders(
        {
            "deploy/test_gitops_manual_trigger.py": (
                '_RULE = _ROLE / "templates/50-gitops-deploy.rules.j2"\n'
                'assert "polkit.Result.YES" in _RULE.read_text()\n'
            ),
            "deploy/test_already_clean.py": (
                "from _setup_render import rendered_setup_text\n"
                'assert "YES" in rendered_setup_text("gitops_deploy", "50-gitops-deploy.rules.j2")\n'
            ),
        }
    )
    assert list(found) == ["deploy/test_gitops_manual_trigger.py"], found


def test_a_reverted_k8s_guard_is_named_as_an_offender():
    """The directory added in #3203, against the read its flaresolverr guard took until then.

    `test_flaresolverr_disable_media.py` checked that the template SPELLS the default's name;
    it renders at a flipped default instead. Putting the read back must fail the census.
    """
    found = offenders(
        {
            "k8s/test_flaresolverr_disable_media.py": (
                'source = ROLES / "k8s" / "prowlarr" / "templates" / _TEMPLATE\n'
                'assert "prowlarr_k8s_fs_disable_media" in source.read_text()\n'
            ),
            "k8s/test_already_clean.py": (
                "from _k8s_render import render_role_template\n"
                'assert "false" in render_role_template("prowlarr", "x.yaml.j2", {})\n'
            ),
        }
    )
    assert list(found) == ["k8s/test_flaresolverr_disable_media.py"], found
