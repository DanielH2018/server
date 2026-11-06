"""Which sibling modules each module of the GitOps deployer's `files/` may import.

`test_gitops_deploy_imports.py` checks this table against what is on disk and against every
module's imports. It lives here only to keep that file under the test-module length cap.
"""

# The entry point, the one module with no import restriction.
ENTRY = "gitops_deploy"

# Every module in files/, and which siblings each may import. `None` means no restriction, and
# only the entry module gets it. The module SET is checked with `==` against what is on disk, so
# a new module with no entry here fails; each module's imports are checked with `<=`.
#
# DECIDED: keep the per-module allowlist rather than narrow it to "no cycles, nothing imports the
# entry module" (#4398). No commented copy between deployer modules exists because ALLOWED lacks
# an edge. Each comes from a real cycle (`gitops_markers.owed_clear_cmd`'s "manual_plane"
# literal), a module that ships alone (`gitops_markers`, `gitops_hold`), host_lib purity
# (`deploy_remediation.BRANCH_DEFAULT`), or a source outside files/ under `--no-project`
# (`deploy_phases.SHARED_K8S_ROLES`, with its own sync test), so the narrower rule would fold
# none of them. A copy behind a non-cycle edge folds by adding the edge here, as
# `DEPLOY_PLAYBOOK` did (#4428).
ALLOWED: dict[str, set[str] | None] = {
    # The cross-role tables, a leaf of their own because the tick and land.sh parse another
    # commit's copy of that file by path.
    "deploy_changes": {"deploy_cross_role", "deploy_setup_roles"},
    "deploy_cross_role": set(),
    # Which playbook and tag apply a setup role, answered for a role name rather than a path.
    # Stdlib only: it runs the derivation as a subprocess (#3734).
    "deploy_setup_roles": set(),
    # The setup narrowing subprocess and the plan it returns: `deploy_config` for `log`, and
    # `gitops_hold` for `held_tag`, the hold-marker format `broad_hold_cleared_by` reads back
    # (#3138) — a stdlib leaf, so no cycle. It must NOT import `deploy_toolbox`, which imports
    # IT for the `narrow_setup_role` default.
    "deploy_narrow": {
        "deploy_config",
        "deploy_setup_roles",
        "gitops_hold",
    },
    # The state directory, the marker table and the line parsers. Import-free by construction:
    # a copy of it ships into every other tree that reads the markers (its header says how),
    # so an import here would have to be satisfiable in a monitor-bridge pod.
    "gitops_markers": set(),
    # The JSON-lines markers (the owed-work ledger and the tick receipt). Reads
    # `gitops_markers` for the `K8sDeferredEntry` tuple and nothing else.
    "gitops_ledger": {"gitops_markers"},
    # The hold and the rule that clears it (#3658). The deploy UI installs it beside the two
    # modules above, so it may import nothing else.
    "gitops_hold": {"gitops_ledger", "gitops_markers"},
    # The tick's textfile metric (#5676). The unit's `ExecStopPost=` runs it as a script on a
    # tick that never reached the entry point, so it reads the behind marker and nothing more.
    "gitops_tick_metrics": {"gitops_markers"},
    "deploy_config": set(),
    "deploy_failtext": set(),
    "deploy_git": set(),
    "deploy_health": set(),
    "deploy_inventory": {"deploy_changes"},
    # The per-service flocks. A leaf: `scripts/deploy.sh` takes the same locks by the same
    # names, and keeping this module free of the deployer's config is what lets a test drive
    # it directly.
    "deploy_locks": set(),
    # `gitops_ledger` for the `manual_plane` class name its clear command prints (#3547), and
    # `deploy_cross_role`, a leaf, for the roles that render `common`'s files (#4316).
    # `deploy_setup_roles`, a stdlib leaf, for the agent hosts the k3s join command names
    # (#5606); `deploy_changes` re-exports its other names but sits at its length cap.
    "deploy_remediation": {
        "deploy_changes",
        "deploy_cross_role",
        "deploy_setup_roles",
        "gitops_ledger",
        "gitops_markers",
    },
    # The broad arm's two deferral shapes — park, or record in `manual_plane`. It reaches the
    # alert transport, so it sits with `deploy_handlers` rather than among the pure modules.
    "deploy_defer": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_narrow",
        "deploy_remediation",
        # `SecretReadersRefused`, the reader script's refusal (#5436); a stdlib leaf.
        "deploy_setup_roles",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
        "gitops_markers",
    },
    "deploy_tick_types": {"deploy_changes"},
    # The marker files, plus `behind_marker` from `deploy_git`, the hold's owner
    # `gitops_hold`, and `deploy_config` for `log`.
    "deploy_state": {
        "deploy_config",
        "deploy_git",
        "gitops_hold",
        "gitops_ledger",
        "gitops_markers",
    },
    "deploy_io": {
        "deploy_config",
        "deploy_failtext",
        "deploy_health",
        "deploy_inventory",
        "deploy_locks",
        "deploy_state",
        "gitops_markers",
    },
    # The boundaries object: `deploy_io` for the transport, `deploy_git` for the CI verdict,
    # `deploy_config` for the Config it binds. It holds `post`, the webhook itself, so that
    # `deploy_alerts` can import IT rather than the other way round. Never `gitops_deploy` —
    # that is rule 1.
    "deploy_toolbox": {
        "deploy_config",
        "deploy_git",
        "deploy_io",
        "deploy_narrow",
        "deploy_setup_roles",
    },
    # Every message body, and nothing that sends one: the ChangeSet its signatures take, the
    # error-text slicers and the k8s remediation it appends. No transport, no state:
    # `gitops_markers` is the leaf that names marker files and clear commands, and the alert
    # takes `HOLD_CLEAR_CMD` from it (#4015).
    "deploy_alert_text": {
        "deploy_changes",
        "deploy_failtext",
        "deploy_remediation",
        "gitops_markers",
    },
    "deploy_alerts": {
        "deploy_alert_text",
        "deploy_changes",
        "deploy_config",
        "deploy_health",
        "deploy_io",
        "deploy_remediation",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    "deploy_phases": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_cross_role",
        # `drop_deleted_setup_roles`, which `plan_tick` asks beside the k8s drop (#4326).
        "deploy_defer",
        "deploy_setup_roles",
        "deploy_git",
        "deploy_inventory",
        "deploy_io",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    "deploy_handlers": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_defer",
        "deploy_narrow",
        "deploy_changes",
        "deploy_config",
        "deploy_git",
        "deploy_health",
        "deploy_io",
        "deploy_locks",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    # The index over the decision modules.
    "deploy_logic": {
        "deploy_changes",
        "deploy_git",
        "deploy_health",
        "deploy_inventory",
        "deploy_remediation",
    },
    ENTRY: None,
}
