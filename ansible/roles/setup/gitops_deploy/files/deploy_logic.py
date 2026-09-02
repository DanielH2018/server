# ansible/roles/setup/gitops_deploy/files/deploy_logic.py
"""Pure decision logic for the GitOps deployer (no I/O — unit-tested).

This module is the index: every decision the deployer makes is defined in one of the
`deploy_*` modules beside it, grouped by the question it answers, and re-exported here so
`gitops_deploy.py` imports one name and the docs' `deploy_logic.<name>` citations stay true.

| module | decides |
|---|---|
| `deploy_changes` | which services and planes a pushed path list reaches (`ChangeSet`) |
| `deploy_remediation` | the text a deferred change's alert prescribes |
| `deploy_git` | what a tick does given the two HEADs, the hold and the CI verdict |
| `deploy_health` | the Docker health gate and the Discord delivery queue |
| `deploy_inventory` | what this host declares, parsed from host_vars text |
| `deploy_k8s` | k8s auto-deploy eligibility, the denylist, the rollback's revert note |
| `deploy_staging` | the staging subset and its verdict summary |

Nothing defines a name here, and that is load-bearing for the tests: a `monkeypatch` on
`deploy_logic.<name>` rebinds a re-export that no function reads, so the test passes against
unpatched code. `ansible/tests/deploy/test_gitops_deploy_patch_boundary.py` refuses such a patch —
patch the module whose function reads the name, exactly as monitor-bridge's split prescribes.
"""

from deploy_changes import (  # noqa: F401
    _ACTIVE_CONFIG,
    _ACTIVE_K8S,
    _ACTIVE_META,
    _ACTIVE_ROLE,
    _ACTIVE_TASKS,
    _BROAD_DEPLOY_PREFIXES,
    _BROAD_MANUAL_PREFIXES,
    _BROAD_SETUP_PREFIXES,
    _BUILD_ROLL_COUPLINGS,
    _SECRETS_FILE,
    _SETUP_ROLE,
    _SETUP_ROLE_TAG_OVERRIDES,
    _SETUP_ROLES_OUTSIDE_INITIAL_SETUP,
    ChangeSet,
    _is_test_only_path,
    _note_setup_role,
    comment_only_broad_changes,
    expand_build_couplings,
    services_from_changed_paths,
    setup_role_playbook,
    setup_role_tag,
    setup_tags_for,
    shared_module_consumers,
)
from deploy_git import (  # noqa: F401
    _CI_NO_VERDICT_CONCLUSIONS,
    _CI_PASS_CONCLUSIONS,
    behind_marker,
    ci_verdict,
    dirty_alert_slot,
    dirty_summary,
    github_auth_headers,
    github_token,
    is_diverged,
    next_action,
    should_alert_dirty,
)
from deploy_health import (  # noqa: F401
    _CONTAINER_NAME,
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
    container_names,
    containers_to_gate,
    gate_services,
    health_decision,
    health_settles,
)
from deploy_inventory import (  # noqa: F401
    _DECLARED_ENTRY,
    _ENTRY_PLATFORM,
    declared_k8s_services,
    declared_services,
    reroute_k8s_services,
    stale_rendered_services,
)
from deploy_k8s import (  # noqa: F401
    _DECLARATION_RE,
    _DIFF_HEADER,
    _DIFF_IMAGE_LINE,
    _K8S_DEFAULTS_PATH,
    _SNAPSHOT_CLAIM_RE,
    _TRUE_VALUES,
    SHARED_K8S_ROLES,
    declared_denylist,
    declares_snapshot_claims,
    is_image_only_diff,
    k8s_role_paths,
    rollback_volume_revert_note,
    split_k8s_auto_deploy,
)
from deploy_remediation import (  # noqa: F401
    BRANCH_DEFAULT,
    BROAD_BUDGET_MARGIN_S,
    _setup_commands,
    broad_budget_ok,
    broad_remediation,
    deferred_service_alerts,
    k8s_remediation,
)
from deploy_staging import staging_scope, staging_verdict_summary  # noqa: F401
