"""A guard under a SCANNED test directory asserts on a RENDER, never on a template's text.

#2809 settled this as prose — "no `tests/services` test asserts a literal copied from
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
byte-identical templates — both render differently from what they test — and a third
(#3452): whether a tiered role passes `pod_shell` a literal class. `staging/` joined
with none (#3205). `hosts/tests/` joined, as `tests/setup/`, with two, each keyed on
something no render carries: a Jinja FILTER, and the behaviour of a variable no render leaves
undefined (#3202). Every test directory is scanned. Entries are keyed `<label>/<module>.py`,
where the label is the directory's key in `SCANNED`, because a basename alone would let a
module in one directory inherit another's exemption.

The exposure is the same one the `tests-read-shell-templates-rendered` row of
`scripts/tests/test_census_rows_test_renders.py` covers for `*.sh.j2` repo-wide: a value that
moves into a role default leaves the assertion matching `{{ ... }}`, and a pattern that
matches nothing passes (#3178). The sanctioned readers are `_k8s_render` (k8s manifests and
`Dockerfile*.j2`), `_setup_render` (setup-plane templates) and `_shell_render` (`*.sh.j2`).

Two things the rule deliberately does not reach:

* **A role's `files/` copy.** It ships verbatim, so there is no render to read and the text
  IS the artifact.
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
#3224). The fifth, the checksum-annotation census, read its template half out of
`_k8s_render` (#3225) until it retired with its last subject (#5644).

`tests/repo/_render_gate.py` holds the detector itself — which spellings of a read it
resolves, and the one it deliberately does not treat as a read (a glob that only yields
FILENAMES).

Run: uv run pytest tests/repo/test_guard_tests_read_renders_not_templates.py
"""

import functools
from pathlib import Path

from lib.repo_paths import REPO, TESTS

from _render_gate import sibling_sources, template_source_reads

# Every directory the rule covers, by the label its keys carry. Adding one means converting its
# readers first.
SCANNED = {
    "services": TESTS / "services",
    "longhorn": TESTS / "longhorn",
    "repo": TESTS / "repo",
    "deploy": TESTS / "deploy",
    "k8s": TESTS / "k8s",
    "staging": TESTS / "staging",
    "hosts/tests": REPO / "hosts" / "tests",
}

# Modules that read a template's SOURCE, each with why a render cannot answer the question.
# Every entry is permanent: a render erases the thing it reads.
TEMPLATE_SOURCE_READERS = {
    "hosts/tests/test_docs_refresh_failure_path.py": (
        "the tmpfiles `d` line's owner and mode are the subject, and the file is a one-line "
        "package path, not a shell script with a render to run"
    ),
    "k8s/test_priority_class_is_derived_from_tier.py": (
        "whether a tiered role's `pod_shell(...)` call passes a literal class or the derived "
        "one — both render the same `priorityClassName`, so only the source tells them apart"
    ),
    "services/test_n8n_build_is_pinned.py": (
        "Renovate's `matchString` runs against `Dockerfile.j2` on disk, braces intact; a "
        "render is not what Renovate sees"
    ),
    "services/test_nut_fsd_login_confined.py": (
        "which ROLES name `nut_monitor_password` — every render of that secret is the same "
        "`STUB`, so the census has to key on the variable name"
    ),
    "services/test_qbittorrent_mullvad_account_secret.py": (
        "the account must render with an int `mullvad_account`, as SOPS types it, and "
        "`_k8s_render`'s stubs are strings; the to_json red case also mutates the source"
    ),
    "services/test_storage_rule_literals.py": (
        "which ROLES write `snapshotMaxSize` in a verify Job — the census keys on the field "
        "NAME, which a render does not change, across every role's verify templates"
    ),
    "services/test_smtp_wiring.py": (
        "a `# DECIDED:` comment in authelia's config-secret template, which no render carries"
    ),
    "services/test_strangler_bridge.py": (
        "the ABSENCE of a retired variable name (`bridge_hostname`) across every template, "
        "which a render cannot show — an undefined name renders as nothing"
    ),
    "deploy/_autodeploy.py": (
        "the fallback for a role no render reaches: the synthetic `widget_role` trees under "
        "`tmp_path` the derivation's unit tests build, and the caller-rendered roles "
        "`_k8s_render` skips, all denylisted. Every role the render reaches reads its render, "
        "and `test_the_render_reaches_every_auto_deployable_role` holds the denylisted half"
    ),
    "deploy/test_tree_lock_single_definition.py": (
        "whether a host cron CALLS git-tree-lock.j2's macro or writes the `exec 9>` idiom out "
        "by hand; both render the same two lines, so only the source tells them apart. The "
        "rendered wait is checked by gitops_deploy's timeout-budget census"
    ),
    "k8s/test_arr_deployments_share_one_macro.py": (
        "whether radarr's and sonarr's templates CALL the shared macro or write its body out "
        "beside the call; both render the same Deployment, so only the source tells them "
        "apart. The macro's body itself is checked on the render"
    ),
    "k8s/test_crowdsec_agent_sidecar_shares_one_macro.py": (
        "whether traefik's and authelia's templates CALL the shared CrowdSec agent macros or "
        "write a container or volume out beside them; both render the same pod, so only the "
        "source tells them apart. The map the macros read is checked on the render"
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
    "longhorn/test_longhorn_reap_never_scheduled.py": (
        "the host components' unit templates, which no render covers, and the synthetic "
        "`.service.j2` and `.timer.j2` under `tmp_path` the red proofs write. A schedule is "
        "forbidden by name, so the source is the subject"
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
        "the fallback for the two templates no render reaches: `k8s/image-builder`'s build "
        "Job and context ConfigMap, both rendered by an `include_role` helper whose "
        "variables arrive on the caller's include task. The census itself spans "
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
    "hosts/tests/test_nut_host_secondary.py": (
        "whether the two push-token expressions carry `| mandatory`, which decides whether an "
        "undefined token fails the play or renders empty. A render resolves every secret to "
        "`STUB`, so no render distinguishes `mandatory` from `default('')`"
    ),
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the directory is clean.
KNOWN_MEMBERS = frozenset(
    {
        "services/_homepage_config.py",
        "services/_jellyfin_plugins.py",
        "services/test_authelia_access_tiers.py",
        "services/test_monitor_bridge_modules.py",
        "services/test_smtp_wiring.py",
        "services/test_strangler_bridge.py",
        "longhorn/_restore_drill.py",
        "longhorn/test_daily_group_membership_is_the_r2_list.py",
        "longhorn/test_longhorn_reap_never_scheduled.py",
        "longhorn/test_longhorn_restore_drill_byte_floor.py",
        "repo/_render_gate.py",
        "repo/test_census_rows_roles.py",
        "repo/test_render_gate.py",
        "repo/test_guard_tests_read_renders_not_templates.py",
        "repo/test_testpaths_covers_every_test_file.py",
        "deploy/_autodeploy.py",
        "deploy/_autodeploy_claims.py",
        "deploy/test_k8s_autodeploy_rollout_gates.py",
        "k8s/_manifest_guards.py",
        "k8s/test_arr_deployments_share_one_macro.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "k8s/test_container_security_context_uses_the_macro.py",
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
        "hosts/tests/test_claude_code_on_daniel_server.py",
        "hosts/tests/test_claude_memory_sync.py",
        "hosts/tests/test_claude_rc_unit.py",
        "hosts/tests/test_coredns_metrics_binds_wildcard.py",
        "hosts/tests/test_etcd_metrics_single_switch.py",
        "hosts/tests/test_k3s_control_plane_hardening.py",
        "hosts/tests/test_loki_route_witness.py",
        "hosts/tests/test_nut_host_secondary.py",
    }
)


def _modules() -> dict[str, Path]:
    """Every scanned module, by its `<label>/<module>.py` key."""
    return {
        f"{label}/{p.name}": p
        for label, directory in SCANNED.items()
        for p in sorted(directory.glob("*.py"))
        if p.name != "__init__.py"
    }


@functools.cache
def _importable(label: str) -> tuple[tuple[str, str], ...]:
    """Every module a test in the `label` directory can import, as (module name, text) pairs.

    Cached as a tuple because `functools.cache` needs a hashable return value and the census
    asks for the same directory once per module in it.
    """
    return tuple(sibling_sources(SCANNED[label], TESTS).items())


def test_the_census_reaches_every_known_module():
    missing = KNOWN_MEMBERS - set(_modules())
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_listed_module_still_exists():
    """A stale entry lets a whole module back out of the rule."""
    found = _modules()
    gone = sorted(name for name in TEMPLATE_SOURCE_READERS if name not in found)
    assert gone == [], f"listed modules that no longer exist: {gone}"


def _label(key: str) -> str:
    return key.rpartition("/")[0]


def offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    """The modules in `sources` (name -> text) that read a template and are not exempt.

    Takes the sources rather than reading the directory, so the red proof below can hand it a
    module that does not exist on disk: a gate only ever observed passing is a gate with no
    evidence it can fail.
    """
    by_directory: dict[str, dict[str, str]] = {}
    for name, text in sources.items():
        by_directory.setdefault(_label(name), {})[Path(name).stem] = text
    return {
        name: reads
        for name, text in sorted(sources.items())
        if name not in TEMPLATE_SOURCE_READERS
        and (
            reads := template_source_reads(
                text,
                dict(_importable(directory := _label(name))) | by_directory[directory],
            )
        )
    }


def test_no_services_test_reads_a_templates_source():
    found = offenders({key: p.read_text() for key, p in _modules().items()})
    assert found == {}, (
        "these tests assert on a template's SOURCE, so a value moved into a role default "
        "leaves the assertion matching `{{ ... }}`. Render it instead — `_k8s_render` for a "
        "manifest or a `Dockerfile*.j2`, "
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
            dict(_importable(_label(name))),
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


def test_a_new_host_guard_reading_a_role_template_is_named_as_an_offender():
    """The directory added in #3202, and the reason the keys carry one.

    `hosts/tests/test_nut_host_secondary.py` is exempt, so a basename-keyed exemption would
    wave through a `services/` module of the same name. Both halves are modules that are not on
    disk, which is what makes this a proof the gate can fail.
    """
    source = (
        'T = SETUP_ROLES / "claude_code" / "templates" / "claude-rc.service.j2"\n'
        'assert "Slice=" in T.read_text()\n'
    )
    found = offenders(
        {
            "hosts/tests/test_new_guard.py": source,
            "services/test_nut_host_secondary.py": source,
            "hosts/tests/test_already_clean.py": "from _setup_render import rendered_setup_text\n"
            'assert "Slice=" in rendered_setup_text("claude_code", "claude-rc.service.j2")\n',
        }
    )
    assert sorted(found) == [
        "hosts/tests/test_new_guard.py",
        "services/test_nut_host_secondary.py",
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
                'source = K8S_ROLES / "prowlarr" / "templates" / _TEMPLATE\n'
                'assert "prowlarr_k8s_fs_disable_media" in source.read_text()\n'
            ),
            "k8s/test_already_clean.py": (
                "from _k8s_render import render_role_template\n"
                'assert "false" in render_role_template("prowlarr", "x.yaml.j2", {})\n'
            ),
        }
    )
    assert list(found) == ["k8s/test_flaresolverr_disable_media.py"], found
