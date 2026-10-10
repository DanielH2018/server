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
| `deploy_health` | the Discord delivery queue's pure half |
| `deploy_inventory` | what this host declares, parsed from host_vars text |
| `deploy_k8s` | k8s auto-deploy eligibility, the denylist, the rollback's revert note |

Nothing defines a name here, and that is load-bearing for the tests: a `monkeypatch` on
`deploy_logic.<name>` rebinds a re-export that no function reads, so the test passes against
unpatched code, and the injected `DeployTools` (`deploy_toolbox.py`) is what a test replaces
instead. `ansible/tests/deploy/test_gitops_deploy_imports.py` holds this file at zero
definitions, and holds every module's sibling imports to its declared set.
"""

# DECIDED: this index stays, and its callers keep importing `deploy_logic` rather than the leaf
# modules (issue #2810 asked whether a pure re-export facade should be dissolved). It IS a pure
# facade — `ansible/tests/deploy/test_gitops_deploy_imports.py` holds it at zero definitions —
# and that is the design rather than an accident. Three things pay for its 105 lines. The table
# above is the only place the deployer's decisions are listed by the question each one answers,
# and a reader who starts at `gitops_deploy.py` reaches it through the single import that module
# makes. About 120 places name `deploy_logic.<symbol>` — docs prose, this role's CLAUDE.md, the
# test suites, and monitor-bridge's copy of `gitops_markers.py` — so dissolving the index
# rewrites every one of them to name whichever leaf a symbol lives in today, which is exactly
# the churn the index absorbs when a symbol moves between leaves. The monkeypatch trap in the
# docstring is the third: a test that patches `deploy_logic.<name>` rebinds a re-export no
# function reads and passes against unpatched code, so this file defining nothing is what keeps
# the injected `DeployTools` the only seam. Contradict this with a measurement that the index
# cost something, not with a line count.

from deploy_changes import (  # noqa: F401
    _BROAD_CENSUS_PREFIXES,
    _BROAD_DEPLOY_PREFIXES,
    _BROAD_MANUAL_PREFIXES,
    _BROAD_PLAY_PREFIXES,
    _BROAD_SETUP_PREFIXES,
    _SECRETS_FILE,
    ChangeSet,
    _is_test_only_path,
    setup_roles_for,
    comment_only_broad_changes,
    is_doc,
    role_of,
    services_from_changed_paths,
    setup_role_host,
    setup_role_playbook,
    setup_role_tag,
    setup_tags_for,
    tick_applies_setup_role,
    _content_lines,
    shared_module_consumers,
)
from deploy_git import (  # noqa: F401
    _CI_NO_VERDICT_CONCLUSIONS,
    _CI_PASS_CONCLUSIONS,
    behind_marker,
    ci_verdict,
    ci_walk_candidates,
    dirty_alert_slot,
    dirty_summary,
    is_diverged,
    next_action,
    should_alert_dirty,
)
from deploy_health import (  # noqa: F401
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
)
from deploy_inventory import (  # noqa: F401
    _DECLARED_ENTRY,
    _ENTRY_PLATFORM,
    declared_k8s_services,
    declares_no_gitops,
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
    _narrowed_tags,
    _setup_commands,
    broad_budget_ok,
    broad_remediation,
    deferred_service_alerts,
    k8s_remediation,
    manual_plane_clear_for,
    manual_plane_remediation,
)
